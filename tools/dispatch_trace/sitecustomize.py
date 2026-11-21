"""Attribute executions of original table callbacks without replacing their function objects."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
import sys
from pathlib import Path

active=os.environ.get("BR_DISPATCH_TEST","")
origin=""
hits={}
sites=set()
phase=""
def mark(site):
    if site:
        hits.setdefault(site,set()).add(active)
    return False
def create(site,factory,*args,**kwargs):
    global origin
    previous=origin
    origin=site
    try:return factory(*args,**kwargs)
    finally:origin=previous
def register(site):
    sites.add(site)
    return site
def call(name,*args):
    return ast.Call(ast.Name(name,ast.Load()),list(args),[])
def tag(site,callback):
    return ast.BoolOp(ast.Or(),[call("_br_dispatch_mark",site),callback])
class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        tree=ast.parse(self.get_data(self.path),filename=self.path)
        rel=fullname.replace(".","/")+".py"
        if fullname=="sqlglot.dialects.dialect":
            fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=="rename_func")
            ret=fn.body[-1]
            assert isinstance(ret,ast.Return) and isinstance(ret.value,ast.Lambda)
            fn.body.insert(-1,ast.Assign([ast.Name("_br_origin",ast.Store())],call("_br_dispatch_origin")))
            ret.value.body=tag(ast.Name("_br_origin",ast.Load()),ret.value.body)
        for cls in (n for n in tree.body if isinstance(n,ast.ClassDef)):
            for stmt in cls.body:
                if not isinstance(stmt,ast.Assign) or len(stmt.targets)!=1 or not isinstance(stmt.targets[0],ast.Name) or stmt.targets[0].id not in {"FUNCTIONS","TRANSFORMS"} or not isinstance(stmt.value,ast.Dict):continue
                for index,node in enumerate(stmt.value.values):
                    site=f"{rel}:{node.lineno}:{node.col_offset}"
                    if isinstance(node,ast.Lambda):
                        sites.add(site)
                        node.body=tag(ast.Constant(site),node.body)
                    elif isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=="rename_func" and len(node.args)==1 and isinstance(node.args[0],ast.Constant) and type(node.args[0].value) is str and not node.keywords:
                        stmt.value.values[index]=ast.copy_location(call("_br_dispatch_create",call("_br_dispatch_register",ast.Constant(site)),node.func,*node.args),node)
        return compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if not fullname.startswith("sqlglot."):return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            spec.loader=Loader(fullname,spec.origin)
        return spec
def save():
    path=Path(os.environ["BR_DISPATCH_OUT"])/(str(os.getpid())+".json.gz")
    path.write_bytes(gzip.compress(json.dumps([sorted(sites),{s:sorted(ids) for s,ids in hits.items()}]).encode(),mtime=0))
if "BR_DISPATCH_OUT" in os.environ:
    builtins._br_dispatch_mark=mark
    builtins._br_dispatch_origin=lambda:origin
    builtins._br_dispatch_create=create
    builtins._br_dispatch_register=register
    sys.meta_path.insert(0,Finder())
    atexit.register(save)
