"""Record when replacing object identity by rich equality could affect execution."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import marshal
import os
import sys
from enum import Enum
from functools import lru_cache
from pathlib import Path

active = os.environ.get("BR_FLAG_TEST","")
loading = 0
sites = set()
uncertain = {}
atoms = (type(None), bool, int, float, complex, str, bytes)
containers = (tuple, list, dict, set, frozenset)

@lru_cache(4096)
def behavior(cls):
    eq, ne = cls.__eq__, cls.__ne__
    if eq is object.__eq__ and ne is object.__ne__:
        return "identity"
    if cls in atoms or issubclass(cls, Enum) and eq in (int.__eq__, str.__eq__) and ne in (str.__ne__, int.__ne__):
        return "scalar"
    if (getattr(eq,"__module__",None) == "sqlglot.expressions.core" and
        getattr(eq,"__qualname__",None)=="Expression.__eq__" and
        getattr(ne,"__module__",None) == "sqlglot.expressions.core" and
        getattr(ne,"__qualname__",None)=="Expression.__ne__"):
        return "expression"
    if cls in containers:
        return "container"
    return "unknown"

def observe(key, a, b, negate):
    same = a is b
    x, y = type(a), type(b)
    left, right = behavior(x), behavior(y)
    if left == right == "identity":
        equivalent = True
    elif left == right == "scalar":
        equivalent = same == (a == b) and (not same) == (a != b)
    elif left == right == "expression":
        equivalent = same or x is not y
    elif left == right == "container" and x is y and same:
        equivalent = True
    elif a is None or b is None:
        equivalent = "unknown" not in (left,right)
    else:
        equivalent = False
    if not equivalent:
        uncertain.setdefault(key,set()).add("*" if loading or not active else active.split("[",1)[0])
    return not same if negate else same

class Rewrite(ast.NodeTransformer):
    def __init__(self,path):
        self.path=path
    def visit_Compare(self,node):
        self.generic_visit(node)
        if len(node.ops)!=1 or not isinstance(node.ops[0],(ast.Is,ast.IsNot)):
            return node
        key=(self.path,node.lineno,node.col_offset)
        sites.add(key)
        return ast.copy_location(ast.Call(ast.Name("_br_equal",ast.Load()),[
            ast.Constant(key),node.left,node.comparators[0],ast.Constant(isinstance(node.ops[0],ast.IsNot))],[]),node)

class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        src=self.get_data(self.path)
        path=str(Path(self.path).relative_to(os.environ["BR_ARG_PIN"]))
        tree=Rewrite(path).visit(ast.parse(src,filename=self.path))
        return compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True)
    def exec_module(self,module):
        global loading
        loading+=1
        try:return super().exec_module(module)
        finally:loading-=1

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=="sqlglot" or fullname.startswith("sqlglot."):
            spec=importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader=Loader(fullname,spec.origin)
            return spec

def save():
    out=Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".bin.gz")
    out.write_bytes(gzip.compress(marshal.dumps((sites,uncertain)),compresslevel=2,mtime=0))

if "BR_FLAG_DIR" in os.environ:
    builtins._br_equal=observe
    sys.meta_path.insert(0,Finder())
    atexit.register(save)
