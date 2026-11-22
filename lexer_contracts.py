"""Compare actual initialized tokenizer configuration at the reads each pristine test made."""

import ast
from functools import lru_cache
import hashlib
import lzma
import marshal
import os
from pathlib import Path
import signal
import sys
import tempfile

import lexical_inputs
import replay_edit

ROOT=Path(__file__).resolve().parent
FIELDS=("single_tokens","keywords","quotes","format_strings","identifiers","comments","string_escapes",
        "byte_string_escapes","identifier_escapes","commands","command_prefix_tokens","tokens_preceding_hint",
        "numeric_literals","var_single_tokens","keyword_trie","unescaped_sequences","numeric_escapes")
TABLES={"KEYWORDS","SINGLE_TOKENS","COMMENTS","NUMERIC_LITERALS","COMMAND_PREFIX_TOKENS","VAR_SINGLE_TOKENS"}
MISSING=("missing",)


@lru_cache(1)
def profile():
    names,masks,sites=marshal.loads(lzma.decompress((ROOT/"maps/tokenizer-reads.xz").read_bytes()))
    return names,{n:1<<i for i,n in enumerate(names)},masks,sites


def declared(tree):
    result={}
    def visit(body,owners=()):
        for s in body:
            if isinstance(s,ast.ClassDef):
                visit(s.body,(*owners,s.name))
                continue
            left=(s.target if isinstance(s,ast.AnnAssign) else s.targets[0]
                  if isinstance(s,ast.Assign) and len(s.targets)==1 else None)
            if owners and isinstance(left,ast.Name) and left.id in TABLES:
                result[(".".join(owners),left.id)]=s
    visit(tree.body)
    return result


def inert(node,token_names):
    if isinstance(node,ast.Constant):
        return type(node.value) in (str,int,bool,type(None))
    if isinstance(node,ast.Attribute):
        return isinstance(node.value,ast.Name) and node.value.id=="TokenType" and node.attr in token_names
    if isinstance(node,(ast.Tuple,ast.List,ast.Set)):
        return all(inert(v,token_names) for v in node.elts)
    return False


def skeleton(node,tokens):
    if isinstance(node,ast.Dict):
        return "dict",tuple((ast.dump(k) if k is not None else None,ast.dump(v))
                            for k,v in zip(node.keys,node.values) if k is None or not(inert(k,tokens) and inert(v,tokens)))
    if isinstance(node,(ast.Set,ast.List,ast.Tuple)):
        return type(node).__name__,tuple(ast.dump(v) for v in node.elts if not inert(v,tokens))
    return None


def describe(selector,text):
    patches=selector.parse_patch(text)
    if len(patches)!=1:
        return
    p=patches[0]
    path=p["old"]
    if (not path or path!=p["new"] or path not in selector.SRC or
            not (path=="sqlglot/tokens.py" or path.startswith("sqlglot/dialects/"))):
        return
    old=selector.SRC[path]
    new, removed, added=selector.apply(old,p["hunks"])
    if not removed and not added:
        return
    original=selector.source_tree(path)
    patched=ast.parse(new)
    left,right=declared(original),declared(patched)
    changes=[]
    for key in left.keys() & right.keys():
        a,b=left[key],right[key]
        if ast.dump(a.value)==ast.dump(b.value):
            continue
        before=skeleton(a.value,lexical_inputs.token_types(selector))
        if before is None or before!=skeleton(b.value,lexical_inputs.token_types(selector)):
            return
        changes.append(key)
        b.value=a.value
    if not changes or ast.dump(original)!=ast.dump(patched):
        return
    # An indel can shift positions returned by inspect even outside the edited declaration.
    inspected=0
    for line in set(removed)|set(added):
        inspected|=1<<max(0,line)
    if len(removed)!=len(added):
        first=min([*removed,*added])
        inspected|=((1<<max(0,len(old.splitlines())+1-first))-1)<<first
    return path,new,changes,inspected


def freeze(value):
    if type(value) in (str,int,float,bool,type(None)):
        return value
    if isinstance(value,int) and type(value).__name__=="TokenType":
        return "token",value.name
    if type(value) is dict:
        return {freeze(k):freeze(v) for k,v in value.items()}
    if type(value) in (set,frozenset):
        return frozenset(freeze(v) for v in value)
    if type(value) in (tuple,list):
        return tuple(freeze(v) for v in value)
    raise TypeError(type(value).__name__)


def take_snapshot():
    from sqlglot.dialects import Dialect,Dialects
    from sqlglot.parser import Parser
    from sqlglot.tokens import Tokenizer
    dialects=[Dialect.get_or_raise(name) for name in Dialects]
    # Resolve subclasses outside the public registry (including Athena's two lexical engines).
    owners={type(d.tokenizer()):d for d in dialects}
    wanted={cls for cls,_ in profile()[-1]}
    seen=set()
    stack=[Tokenizer]
    result={}
    untracked={}
    while stack:
        cls=stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        stack.extend(cls.__subclasses__())
        name=cls.__module__+":"+cls.__qualname__
        if name not in wanted:
            continue
        instance=cls(dialect=owners.get(cls))
        core=instance._core
        result[name]={field:freeze(getattr(core,field)) for field in FIELDS}
        # Capture scalar behavioral switches and all immutable configuration not covered by key reads.
        ignored={"sql","size","tokens","_start","_current","_line","_col","_comments","_char","_end",
                 "_peek","_prev_token_line"}
        untracked[name]={field:freeze(getattr(core,field)) for field in type(core).__slots__
                         if field not in FIELDS and field not in ignored}
    if result.keys()!=wanted:
        raise ValueError(("Missing concrete tokenizer",wanted-result.keys()))
    indirect=[(type(d).__module__,type(d).__qualname__,d.SUPPORTS_COLUMN_JOIN_MARKS,
               freeze(d.parser_class.RESERVED_TOKENS)) for d in dialects]
    return result,(untracked,indirect,freeze(Parser.RESERVED_TOKENS))


@lru_cache(1)
def baseline():
    return marshal.loads((ROOT/"cache/lexer-configs").read_bytes())


def order_value(v):
    if type(v) is dict:
        return "dict",tuple((order_value(k),order_value(x)) for k,x in v.items())
    return v


def observe(table,path,op):
    obj=table
    for key in path[:-1] if op in ("has","get","kind") else path:
        if type(obj) is not dict:
            return MISSING
        obj=obj.get(key,MISSING)
    if op=="all":
        return order_value(obj)
    if op=="truth":
        return bool(obj)
    key=path[-1]
    if op=="has":
        return key in obj if type(obj) in (frozenset,dict) else MISSING
    if type(obj) is not dict:
        return MISSING
    value=obj.get(key,MISSING)
    if op=="kind":
        return ("child",) if type(value) is dict else ("value",value)
    if op=="get":
        return order_value(value)
    raise ValueError(op)


def affected(actual):
    old,extra=baseline()
    new,other=actual
    if extra!=other or old.keys()!=new.keys():
        return
    _,_,masks,sites=profile()
    kept=0
    for cls,before in old.items():
        for field,first in before.items():
            last=new[cls][field]
            if first==last and (type(first) is not dict or tuple(first)==tuple(last)):
                continue
            for (path,op),index in sites.get((cls,field),{}).items():
                if observe(first,path,op)!=observe(last,path,op):
                    kept|=masks[index]
    return kept


def run(path,source):
    with tempfile.TemporaryFile() as out:
        child=os.fork()
        if child==0:
            try:
                signal.signal(signal.SIGPROF,signal.SIG_DFL)
                signal.signal(signal.SIGALRM,signal.SIG_DFL)
                signal.setitimer(signal.ITIMER_PROF,1.7)
                signal.alarm(4)
                root=ROOT/"cache/pinned"
                sys.path.insert(0,str(root))
                sys.meta_path.insert(0,replay_edit.Overlay(path,source,root))
                bits=affected(take_snapshot())
                if bits is not None:
                    out.write(marshal.dumps(bits))
                    out.flush()
            finally:
                os._exit(0)
        os.waitpid(child,0)
        out.seek(0)
        data=out.read()
        return marshal.loads(data) if data else None


@lru_cache(20)
def test_files(fields):
    files=set()
    root=ROOT/"cache/pinned"
    for path in (root/"tests").rglob("*.py"):
        text=path.read_text()
        if any(n in text for n in fields):
            tree=ast.parse(text)
            if any(isinstance(n,ast.Attribute) and n.attr in fields or
                   isinstance(n,ast.Constant) and type(n.value) is str and n.value in fields for n in ast.walk(tree)):
                files.add(path.relative_to(root).as_posix())
    return files


def refine(selector,text,selected):
    if not selected or "sqlglot" in sys.modules or not (ROOT/"cache/lexer-configs").is_file():
        return selected
    if not any(w in text for w in ("TokenType.","KEYWORDS","SINGLE_TOKENS","COMMENTS","NUMERIC_LITERALS",
                                    "COMMAND_PREFIX_TOKENS","VAR_SINGLE_TOKENS")):
        return selected
    try:
        change=describe(selector,text)
        if not change:
            return selected
        path,new,declared_fields,inspect=change
        names,bits,_,_=profile()
        if set(names)!=set(selector.NUM):
            return selected
        kept=run(path,new)
        if kept is None:
            return selected
        saved=set(replay_edit.preserve_scope(selector,path,inspect,selected))
        saved.update(selector.tests_of(selector.M["opens"].get(path)))
        saved.update(n for n,(raw,_) in selector.EXTRA.items() if path in raw)
        fields=tuple(sorted({field for _,field in declared_fields}))
        for name in test_files(fields):
            saved.update(selector.BY_FILE.get(name,()))
        return {n for n in selected if n in saved or kept & bits[selector.TEST_FILE_ID[n]]}
    except (OSError,TypeError,KeyError,ValueError,IndexError,AttributeError,RecursionError,SyntaxError):
        return selected
