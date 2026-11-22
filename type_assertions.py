"""Check the affected SQL type assertions with the original optimizer and equality."""
import ast
from functools import lru_cache
import gzip
import hashlib
import marshal
import os
from pathlib import Path
import signal
import sys

import instruction_impact
import workload_replay

ROOT=Path(__file__).resolve().parent
NODE="tests/test_optimizer.py::TestOptimizer::test_annotate_funcs"
spent=0.0


@lru_cache(1)
def capture():
    return marshal.loads(gzip.decompress((ROOT/"maps/type-assertions.gz").read_bytes()))


def affected(selector,text):
    prepared=workload_replay.prepare(selector,text)
    if not prepared:
        return
    edits,details,opaque,fixtures=prepared
    if fixtures or opaque or selector.NUM[NODE] in workload_replay.file_readers(selector,edits):
        return
    version,rows,schema,masks,seen=capture()
    cv,numbers,known,cold=instruction_impact.observations()
    if tuple(version)!=tuple(cv) or tuple(cv)!=sys.version_info[:2] or not numbers.get(NODE,0)&known:
        return
    bits=0
    for key,changed in details:
        path,name,line,code=key
        if path not in cold:
            return
        if any(other_name==name and other_line==line and other_code!=code and any(tests&numbers[NODE] for tests in visits.values())
               for (other_name,other_line,other_code),visits in cold[path].items()):
            return
        actual=cold[path].get((name,line,code),{})
        for visited,tests in actual.items():
            if tests&numbers[NODE] and visited&changed:
                if key not in seen:
                    return
                bits|=masks[seen[key]]
                break
    if bits&1:
        return
    return edits,bits>>1


def run(edits,bits):
    import unittest
    from replay_edit import Overlay
    pin=ROOT/"cache/pinned"
    sys.path.insert(0,str(pin))
    for p,s in edits.items():
        sys.meta_path.insert(0,Overlay(p,s,pin))
    import sqlglot
    from sqlglot import exp,parse_one
    from sqlglot.optimizer.annotate_types import annotate_types
    from sqlglot.schema import MappingSchema
    from tests.helpers import load_sql_fixture_pairs
    tree=ast.parse((pin/"tests/test_optimizer.py").read_bytes())
    expected={"parse_and_optimize","annotate_functions"}
    functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in expected]
    if {n.name for n in functions}!=expected:
        return False
    bindings=dict(parse_one=parse_one,exp=exp,annotate_types=annotate_types)
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(pin/"tests/test_optimizer.py"),"exec"),bindings)
    sqlglot.schema=MappingSchema()
    _,rows,schema,_,_=capture()
    actual=[("SELECT "+sql+" FROM tbl",expected_type,dialect)
            for meta,sql,expected_type in load_sql_fixture_pairs("optimizer/annotate_functions.sql")
            for dialect in (meta.get("dialect") or "").split(", ")]
    if actual!=rows:
        return False
    while bits:
        bit=bits&-bits
        bits-=bit
        sql,expected_type,dialect=rows[bit.bit_length()-1]
        result=bindings["parse_and_optimize"](bindings["annotate_functions"],sql,dialect,schema=schema,dialect=dialect)
        if result.type.sql(dialect)!=exp.DataType.build(expected_type,dialect=dialect).sql(dialect):
            return False
    return True


def checked(edits,bits,budget):
    if "sqlglot" in sys.modules or not hasattr(os,"fork"):
        return False
    child=os.fork()
    if child==0:
        status=1
        try:
            with open(os.devnull,"wb") as sink:
                os.dup2(sink.fileno(),1)
                os.dup2(sink.fileno(),2)
            signal.signal(signal.SIGPROF,signal.SIG_DFL)
            signal.signal(signal.SIGALRM,signal.SIG_DFL)
            signal.setitimer(signal.ITIMER_PROF,budget)
            signal.setitimer(signal.ITIMER_REAL,2*budget+1.0)
            if run(edits,bits):
                status=0
        finally:
            os._exit(status)
    return os.waitpid(child,0)[1]==0


def refine(selector,text,sel):
    global spent
    n=selector.NUM.get(NODE)
    if n not in sel or spent>=5.0:
        return sel
    gain=(selector.worker_cost(selector.split(sel))-selector.worker_cost(selector.split(sel-{n})))/1000
    if gain<0.40:
        return sel
    start=workload_replay.cpu()
    try:
        proof=affected(selector,text)
        if proof is None:
            return sel
        edits,bits=proof
        if not bits or checked(edits,bits,min(gain*0.68,5.0-spent,2.0)):
            return sel-{n}
    except (OSError,ValueError,LookupError,SyntaxError,TypeError,RecursionError):
        pass
    finally:
        spent+=workload_replay.cpu()-start
    return sel
