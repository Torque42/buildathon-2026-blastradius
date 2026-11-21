"""Observe actual resolution of scalar configuration attributes on the pin."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

active=os.environ.get("BR_FLAG_TEST","")
seen={}
slots=set()
import_depth=0
class Flag:
    def __init__(self,slot,value):
        self.slot=slot
        self.value=value

    def __get__(self,instance,owner=None):
        seen.setdefault(self.slot,set()).add("" if import_depth else active)
        return self.value


def finish(path,qualname,fields):
    def bind(cls):
        for name in fields:
            if name not in vars(cls):
                continue
            value=vars(cls)[name]
            if type(value) not in (bool,int,str) and value is not None:
                continue
            slot=f"{path}:{qualname}:{name}"
            slots.add(slot)
            setattr(cls,name,Flag(slot,value))
        return cls
    return bind


def scalar(node):
    if isinstance(node,ast.UnaryOp) and isinstance(node.op,(ast.UAdd,ast.USub)):
        node=node.operand
    return isinstance(node,ast.Constant) and (type(node.value) in (bool,int,str) or node.value is None)


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self,module):
        global import_depth
        import_depth+=1
        try:
            return super().exec_module(module)
        finally:
            import_depth-=1

    def get_code(self,fullname):
        if not (fullname.startswith(("sqlglot.generators.","sqlglot.parsers.","sqlglot.dialects.")) or fullname in ("sqlglot.generator","sqlglot.parser")):
            return super().get_code(fullname)
        tree=ast.parse(self.get_data(self.path),filename=self.path)
        rel=fullname.replace(".","/")+".py"
        for cls in tree.body:
            if not isinstance(cls,ast.ClassDef):continue
            fields=[]
            for stmt in cls.body:
                if isinstance(stmt,ast.Assign) and len(stmt.targets)==1:
                    target=stmt.targets[0]
                elif isinstance(stmt,ast.AnnAssign):
                    target=stmt.target
                else:continue
                if isinstance(target,ast.Name) and target.id.isupper() and scalar(stmt.value):
                    fields.append(target.id)
            if fields:
                cls.decorator_list.insert(0,ast.Call(ast.Name("_br_flags",ast.Load()),[ast.Constant(rel),ast.Constant(cls.name),ast.Tuple([ast.Constant(x) for x in fields],ast.Load())],[]))
        return compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True)

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if not fullname.startswith(("sqlglot.","tests.")) and fullname not in ("sqlglot","tests"):
            return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            spec.loader=Loader(fullname,spec.origin)
        return spec

def save():
    data=[sorted(slots),{k:sorted(v) for k,v in seen.items()}]
    (Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".json.gz")).write_bytes(gzip.compress(json.dumps(data).encode(),mtime=0))

if "BR_FLAG_DIR" in os.environ:
    builtins._br_flags=finish
    sys.meta_path.insert(0,Finder())
    atexit.register(save)
