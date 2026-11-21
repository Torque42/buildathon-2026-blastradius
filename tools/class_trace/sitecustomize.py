"""Measure operand identity, metaclass dispatch, and nominal type checks in rich comparisons."""
import ast
import atexit
import builtins
import functools
import gzip
import importlib.abc
import importlib.machinery
import inspect
import marshal
import os
import sys
import types
from pathlib import Path

active=os.environ.get("BR_FLAG_TEST","")
depth=0
sites=set()
observed={}
profiling=False
pin=Path(os.environ.get("BR_ARG_PIN",".")).resolve()

@functools.lru_cache(None)
def protocol(left,right):
    # Inputs are exact runtime types of class objects or None. Reject unusual
    # attribute and instance-check protocols before using their nominal MRO.
    if type.__getattribute__(left,"__getattribute__") not in (object.__getattribute__,type.__getattribute__):
        return
    if any(base is not object and base is not type and "__class__" in type.__getattribute__(base,"__dict__")
           for base in type.__getattribute__(left,"__mro__")):
        return
    eq=type.__getattribute__(left,"__eq__")
    if eq is object.__eq__:
        return ("identity",)
    if type(eq) is not types.FunctionType:return
    if any(eq.__globals__.get(key,getattr(builtins,key)) is not getattr(builtins,key)
           for key in ("type","isinstance","NotImplemented")):
        return
    code=eq.__code__
    if not code.co_filename.startswith(str(pin)+"/sqlglot/"):return
    checks=[]
    for name in code.co_names:
        cls=eq.__globals__.get(name,getattr(builtins,name,None))
        if not isinstance(cls,type):continue
        meta=type(cls)
        if type.__getattribute__(meta,"__instancecheck__") is not type.__instancecheck__:continue
        checks.append((name,any(base is cls for base in type.__getattribute__(left,"__mro__")),any(base is cls for base in type.__getattribute__(right,"__mro__"))))
    return ("function",code.co_filename[len(str(pin))+1:],code.co_firstlineno,tuple(checks))


def observe(key,a,b,negate):
    global profiling
    if profiling:
        return (a is not b) if negate else (a is b)
    profiling=True
    try:
        return snapshot(key,a,b,negate)
    finally:
        profiling=False

def snapshot(key,a,b,negate):
    equal=a is b
    x,y=type(a),type(b)
    row=None
    if (isinstance(a,type) or a is None) and (isinstance(b,type) or b is None):
        # Class objects use their metaclass for rich comparison. The interpreter
        # will resolve which pristine instructions a replacement would visit.
        first=protocol(x,y)
        second=protocol(y,x)
        if first and second:
            row=(equal, x is not y and any(base is x for base in type.__getattribute__(y,"__mro__")), first,second,x is y)
    ctx="*" if depth or not active else active.split("[",1)[0]
    observed.setdefault(key,{}).setdefault(row,set()).add(ctx)
    return (not equal) if negate else equal

class Rewrite(ast.NodeTransformer):
    def __init__(self,path):
        self.path=path
        self.enabled=False
    def visit_FunctionDef(self,node):
        old=self.enabled
        self.enabled=node.name in ("__eq__","__ne__")
        self.generic_visit(node)
        self.enabled=old
        return node
    def visit_Compare(self,node):
        self.generic_visit(node)
        if not self.enabled or len(node.ops)!=1 or not isinstance(node.ops[0],(ast.Is,ast.IsNot)):
            return node
        site=(self.path,node.lineno,node.col_offset)
        sites.add(site)
        fn=ast.Call(ast.Name("_br_cls_observe",ast.Load()),[ast.Constant(site),node.left,node.comparators[0],ast.Constant(isinstance(node.ops[0],ast.IsNot))],[])
        return ast.copy_location(fn,node)
class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,name):
        path=str(Path(self.path).relative_to(pin))
        tree=Rewrite(path).visit(ast.parse(self.get_data(self.path),self.path))
        return compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True)
    def exec_module(self,module):
        global depth
        depth+=1
        try:super().exec_module(module)
        finally:depth-=1
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name=="sqlglot" or name.startswith("sqlglot."):
            spec=importlib.machinery.PathFinder.find_spec(name,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader=Loader(name,spec.origin)
            return spec
def save():
    target=Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".bin.gz")
    target.write_bytes(gzip.compress(marshal.dumps((sites,observed)),2,mtime=0))
if "BR_FLAG_DIR" in os.environ:
    builtins._br_cls_observe=observe
    sys.meta_path.insert(0,Finder())
    atexit.register(save)
