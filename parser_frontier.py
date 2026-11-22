"""Check the actual lexer and initialized parser independently during the original parser test."""

from functools import lru_cache
import gzip
import hashlib
import marshal
import os
from pathlib import Path
import re
import signal
import sys
import tempfile

import binding_state
import declaration_guard
import instruction_impact
import replay_edit
import shortlist
import token_streams
import workload_replay

ROOT=Path(__file__).resolve().parent
spent=0.0


@lru_cache(1)
def profile():
    def read(mode):
        return marshal.loads(gzip.decompress((ROOT/"maps"/("parser-stage-"+mode+".gz")).read_bytes()))
    va,used,globals_,_=read("code")
    vb,_,owners,supers=read("owners")
    if tuple(va)!=sys.version_info[:2] or tuple(vb)!=sys.version_info[:2]:
        return
    return used,{name:sorted(set(keys)|set(globals_[name])) for name,keys in owners.items()},supers


@lru_cache(1)
def witnesses():
    cache=ROOT/"cache/parser-frontiers"
    return marshal.loads(cache.read_bytes()) if cache.is_file() else {}


def stages(selector,text,edits):
    info=profile()
    if info is None:
        return
    used,_,supers=info
    words=set(re.findall(r"\w+"," ".join(line[1:] for f in selector.parse_patch(text)
                                        for h in f["hunks"] for line in h["lines"] if line.startswith(("+","-")))))
    if words & declaration_guard.DYNAMIC:
        return
    eligible={name for name in used if not words.intersection(supers.get(name,()))}
    for path,new in edits.items():
        if not declaration_guard.namespace_changes(selector.source_tree(path).body,declaration_guard.syntax(new).body):
            return
        relevant=[(phase,key,mask) for phase in eligible for key,mask in used[phase].items() if key[0]==path]
        if not relevant:
            continue
        old_code=declaration_guard.compiled(path,selector.SRC[path])
        new_code=declaration_guard.compiled(path,new)
        for phase,(_,name,line,digest),mask in relevant:
            if phase not in eligible:
                continue
            a=[c for c in old_code.get(name,()) if c.co_firstlineno==line and
               hashlib.blake2s(c.co_code,digest_size=12).digest()==digest]
            b=new_code.get(name,())
            if len(a)!=1 or len(b)!=1:
                eligible.discard(phase)
                continue
            if binding_state.code_hash(a[0])!=binding_state.code_hash(b[0]):
                first=instruction_impact.offset_mask(a[0],b[0])
                if first is None or first & mask:
                    eligible.discard(phase)
    return eligible


class Stream:
    def __init__(self,identity):
        self.identity=identity
        self.used=False


def exercise(eligible,reference=None,baseline=False,allow_full=False):
    import unittest
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.parser import Parser
    from sqlglot.errors import ParseError
    from tests.test_parser import TestParser
    original_lex,original_parse=Dialect.tokenize,Parser.parse
    data=witnesses()
    if not data:
        return
    instances={}
    encountered=[]
    stages_ok=set()
    tokens_seen=[]
    fingerprints={}
    def stamp(phase,obj):
        # The installed wrappers are instrumentation. Inspect the original methods including patched overrides.
        Dialect.tokenize,Parser.parse=original_lex,original_parse
        try:
            keys=profile()[1][phase]
            values,unknown=binding_state.live(keys)
            if unknown:
                raise ValueError(("Unresolved stage dependencies",phase))
            fingerprints[phase]=values
            return binding_state.Fingerprints().hash(obj)
        finally:
            Dialect.tokenize,Parser.parse=lex,parse
    def matches(phase,obj,k):
        if phase not in eligible:
            return False
        individual=stamp(phase,obj)
        instances[(phase,k)]=individual
        if baseline:
            return True
        return fingerprints[phase]==reference[0].get(phase) and individual==reference[1].get((phase,k))
    def lex(self,sql,*args,**kw):
        if type(sql) is not str or len(sql)<100_000:
            return original_lex(self,sql,*args,**kw)
        k=token_streams.key(sql)
        if args or kw or k not in data["streams"]:
            raise ValueError("Unexpected lexical invocation")
        if binding_state.code_hash(Dialect.parse.__code__)!=data["bridge"]:
            raise ValueError("Token stream has an unverified consumer")
        if not matches("tokenizer",self,k):
            if not allow_full:
                raise ValueError("Changed lexer")
            actual=original_lex(self,sql)
            if token_streams.encode(actual)!=data["streams"][k]:
                raise ValueError("Actual token stream changed")
        mark=Stream(k)
        tokens_seen.append(mark)
        stages_ok.add("tokenizer")
        return mark
    def parse(self,tokens,sql,*args,**kw):
        if type(tokens) is not Stream:
            return original_parse(self,tokens,sql,*args,**kw)
        k=tokens.identity
        call=(k,self.max_nodes)
        if args or kw or call not in data["errors"] or tokens.used or token_streams.key(sql)!=k:
            raise ValueError("Unexpected initialized parser")
        tokens.used=True
        encountered.append(call)
        if matches("parser",self,call):
            stages_ok.add("parser")
            raise ParseError(data["errors"][call])
        if not allow_full:
            raise ValueError("Changed parser")
        return original_parse(self,token_streams.materialize(data["streams"][k]),sql)
    Dialect.tokenize,Parser.parse=lex,parse
    result=unittest.TestResult()
    try:
        state=shortlist.witness()
        state[:]=[hashlib.blake2s(digest_size=16),0]
        unittest.TestSuite([TestParser("test_max_nodes")]).run(result)
        if (result.testsRun==1 and result.wasSuccessful() and not result.skipped and
                not result.expectedFailures and tuple(encountered)==tuple(data["errors"]) and
                len(encountered)==len(tokens_seen) and all(t.used for t in tokens_seen) and
                (state[1],state[0].digest())==data["assertions"]):
            return (fingerprints,instances) if baseline else True
    finally:
        Dialect.tokenize,Parser.parse=original_lex,original_parse


def fork_run(root,edits,eligible,budget,reference=None,baseline=False,allow_full=False):
    with tempfile.TemporaryFile() as output:
        child=os.fork()
        if child==0:
            try:
                signal.signal(signal.SIGPROF,signal.SIG_DFL)
                signal.signal(signal.SIGALRM,signal.SIG_DFL)
                signal.setitimer(signal.ITIMER_PROF,budget)
                signal.setitimer(signal.ITIMER_REAL,2*budget+0.7)
                os.chdir(root)
                sys.path.insert(0,str(root))
                os.environ["SKIP_INTEGRATION"]="0"
                for path,new in edits.items():
                    sys.meta_path.insert(0,replay_edit.Overlay(path,new,root))
                result=exercise(eligible,reference,baseline,allow_full)
                if result:
                    output.write(marshal.dumps(result))
                    output.flush()
            except Exception:
                pass
            finally:
                os._exit(0)
        os.waitpid(child,0)
        output.seek(0)
        raw=output.read()
        return marshal.loads(raw) if raw else None


@lru_cache(1)
def pristine():
    return fork_run(shortlist.PIN,{},{"tokenizer","parser"},1.8,baseline=True)


def refine(selector,text,selected):
    global spent
    n=selector.NUM.get(workload_replay.NODE)
    if n not in selected or spent>=8.5 or "sqlglot" in sys.modules:
        return selected
    gain=(selector.worker_cost(selector.split(selected))-selector.worker_cost(selector.split(selected-{n})))/1000
    if gain<1.6:
        return selected
    start=workload_replay.cpu()
    try:
        prepared=workload_replay.prepare(selector,text)
        if not prepared or prepared[3]:
            return selected
        edits=prepared[0]
        eligible=stages(selector,text,edits)
        if not eligible:
            return selected
        base=pristine()
        if not base:
            return selected
        with tempfile.TemporaryDirectory(prefix="parser-replay-") as tmp:
            root=Path(tmp)
            shortlist.patched_tree(root,edits)
            budget=min(8.5-spent,0.74*gain,5.5)
            allow_full=budget>=3.4 and eligible!={"parser","tokenizer"}
            if fork_run(root,edits,eligible,budget,base,allow_full=allow_full):
                print("verified initialized parser stages",file=sys.stderr)
                return selected-{n}
    except (KeyError,AttributeError,TypeError,OSError,ValueError,SyntaxError,IndexError,RecursionError):
        pass
    finally:
        spent+=workload_replay.cpu()-start
    return selected
