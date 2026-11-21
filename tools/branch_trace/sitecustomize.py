import ast
import atexit
import builtins
import dis
import gzip
import importlib.abc
import importlib.machinery
import marshal
import os
import sys
import types
from functools import lru_cache
from pathlib import Path
from branch_model import tree

active=os.environ.get("BR_FLAG_TEST","")
states={}
registered={}
missing=object()
atoms=(type(None),bool,int,str,bytes,float)
containers=(list,tuple,str,bytes,dict,set,frozenset)
numeric=(int,float,bool)
@lru_cache(16384)
def desc(cls,k):
    for typ in type.__getattribute__(cls,"__mro__"):
        d=type.__getattribute__(typ,"__dict__")
        if k in d:return d[k]
    return missing

@lru_cache(1024)
def getter(fn):
    # Interpret only explicit direct reads in a pinned, ordinary property getter.
    if not isinstance(fn,types.FunctionType) or not fn.__module__.startswith("sqlglot."):return ()
    instructions=[(i.opname,i.argval) for i in dis.get_instructions(fn) if i.opname!="RESUME"]
    names=[n for n,v in instructions]
    if names==["LOAD_FAST","LOAD_ATTR","RETURN_VALUE"]:return (instructions[1][1],)
    if names==["LOAD_FAST","LOAD_ATTR","LOAD_ATTR","LOAD_CONST","CALL","RETURN_VALUE"] and instructions[2][1]=="get" and instructions[4][1]==1 and type(instructions[3][1]) is str:
        return (instructions[1][1],instructions[3][1])
    return ()

def dict_ok(o):return type(o) is dict and len(o)<=40 and all(type(k) in (str,int,bool,type(None)) for k in o)

def attr(o,k,depth=0):
    if depth>3:return missing
    if type(o) is types.ModuleType:
        return vars(o).get(k,missing)
    if isinstance(o,type):
        # Only ordinary class attributes. Never invoke a metaclass hook.
        if type(o).__getattribute__ is type.__getattribute__ and desc(type(o),k) is missing:
            v=desc(o,k)
            if v is not missing and desc(type(v),"__get__") is missing:return v
        return missing
    cls=type(o)
    if cls.__getattribute__ is not object.__getattribute__:return missing
    value=desc(cls,k)
    if type(value) is types.MemberDescriptorType:
        try:return value.__get__(o,cls)
        except AttributeError:return missing
    if type(value) is property:
        plan=getter(value.fget)
        if not plan:return missing
        v=attr(o,plan[0],depth+1)
        if len(plan)==1:return v
        return v.get(plan[1]) if dict_ok(v) else missing
    if value is not missing and desc(type(value),"__get__") is not missing:return missing
    member=desc(cls,"__dict__")
    if type(member) is types.GetSetDescriptorType:
        d=member.__get__(o,cls)
        return d.get(k,value) if dict_ok(d) else missing
    return value

def boolean(v):
    if v is missing:return missing
    if type(v) in atoms+containers:return bool(v)
    if desc(type(v),"__bool__") is missing and desc(type(v),"__len__") is missing:return True
    return missing

def val(s,f):
    k,*x=s
    if k=="constant":return x[0]
    if k=="opaque":return missing
    if k=="name":
        key=x[0]
        if key in f.f_locals:return f.f_locals[key]
        return f.f_globals.get(key,f.f_builtins.get(key,missing))
    if k=="attr":
        base=val(x[0],f)
        return attr(base,x[1]) if base is not missing else missing
    if k=="tuple":
        vs=tuple(val(e,f) for e in x[0])
        return missing if any(v is missing for v in vs) else vs
    if k=="get":
        base=val(x[0],f);args=tuple(val(e,f) for e in x[1])
        if not dict_ok(base) or type(args[0]) not in (str,int,bool,type(None)) or any(v is missing for v in args):return missing
        return base.get(*args)
    if k=="call":
        name,args=x
        if val(("name",name),f) is not getattr(builtins,name):return missing
        v=val(args[0],f)
        if v is missing:return missing
        if name=="len" and len(args)==1 and type(v) in containers:return len(v)
        if name=="bool" and len(args)==1:return boolean(v)
        if name=="isinstance" and len(args)==2:
            classes=val(args[1],f);classes=classes if type(classes) is tuple else (classes,)
            if all(type(c) is type for c in classes) and desc(type(v),"__class__") is desc(object,"__class__") and type(v).__getattribute__ is object.__getattribute__:
                return any(any(c is m for m in type(v).__mro__) for c in classes)
    return missing

def leaf(s,f):
    a=val(s[1],f)
    if a is missing:return "?"
    if s[0]=="truth":
        t=boolean(a)
        return "?" if t is missing else "T" if t else "F"
    b=val(s[2],f)
    if b is missing:return "?"
    x,y=type(a),type(b)
    eq="?"
    if x in atoms and y in atoms:
        if x is y or x in numeric and y in numeric:
            eq="=" if a==b else "<" if a<b else ">" if a>b else "N"
            if a is None:eq="U"  # equality works, ordering raises
        else:eq="D"
    elif x.__eq__ is object.__eq__ and y.__eq__ is object.__eq__ and x.__ne__ is object.__ne__ and y.__ne__ is object.__ne__:
        eq="I" if a is b else "D"
    member="?"
    # Exact builtins with primitive keys have no observable contains hooks.
    if x in atoms and y in (tuple,list,dict,set,frozenset) and len(b)<=40 and all(type(v) in atoms for v in b):
        member="1" if a in b else "0"
    elif x is y and x in (str,bytes):member="1" if a in b else "0"
    return ("1" if a is b else "0")+eq+member

def visit(site):
    row=states.setdefault(site,{}).setdefault(active,set())
    if None in row:return False
    try:
        f=sys._getframe(1)
        state=tuple(leaf(s,f) for s in registered[site])
        if len(row)>=48 and state not in row:row.clear();row.add(None)
        else:row.add(state)
    except Exception:row.clear();row.add(None)
    return False

class Rewrite(ast.NodeTransformer):
    def __init__(self,path):self.path=path;self.inside=False
    def visit_FunctionDef(self,node):
        old=self.inside;self.inside=True;self.generic_visit(node);self.inside=old;return node
    def visit_If(self,node):
        original=node.test
        leaves=[]
        tree(original,leaves)
        self.generic_visit(node)
        if self.inside and 1<=len(leaves)<=6 and (any(isinstance(n,ast.BoolOp) for n in ast.walk(original)) or os.environ.get("BR_VALUE_PHASE") and isinstance(original,ast.Compare)) and any("'opaque'" not in str(k) for k in leaves):
            site=(self.path,original.lineno,original.col_offset)
            registered[site]=leaves
            node.test=ast.copy_location(ast.BoolOp(ast.Or(),[ast.Call(ast.Name("_br_branch_fact",ast.Load()),[ast.Constant(site)],[]),node.test]),original)
        return node
    visit_IfExp=visit_If
    visit_While=visit_If

class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,name):
        path="sqlglot/"+self.path.split("/sqlglot/",1)[1]
        tr=Rewrite(path).visit(ast.parse(self.get_data(self.path),filename=self.path))
        return compile(ast.fix_missing_locations(tr),self.path,"exec",dont_inherit=True)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name!="sqlglot" and not name.startswith("sqlglot."):return None
        spec=importlib.machinery.PathFinder.find_spec(name,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):spec.loader=Loader(name,spec.origin)
        return spec
def save():
    path=Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".rec")
    path.write_bytes(gzip.compress(marshal.dumps(states),compresslevel=1,mtime=0))

builtins._br_branch_fact=visit
sys.meta_path.insert(0,Finder())
atexit.register(save)
