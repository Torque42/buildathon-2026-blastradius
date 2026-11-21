"""Record required-argument validation and other metadata reads without replacing application objects."""
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

active=os.environ.get("BR_FLAG_TEST","")
imports=[]
events={}
sites=set()
owners={}
def owner(obj):
    cls=obj if isinstance(obj,type) else type(obj)
    if cls in owners:return owners[cls]
    result=None
    for base in cls.__mro__:
        if "arg_types" in vars(base):
            if base.__module__.startswith("sqlglot.expressions.") and type(vars(base)["arg_types"]) is dict:
                result=base.__module__+":"+base.__qualname__
            break
    owners[cls]=result
    return result

def mark(site,key):
    if not site:return
    context=active
    if imports:
        rel=imports[-1]
        context="import:"+rel if rel.startswith("sqlglot/") else "test-import:"+rel if rel.startswith("tests/") else ""
    events.setdefault(site,{}).setdefault(key,set()).add(context)

def view(obj,attr,mode):
    val=getattr(obj,attr)
    slot=owner(obj)
    if not slot:return val
    if mode=="validation":
        args=obj.args
        if type(args) is not dict or type(val) is not set or any(type(k) is not str for k in args):
            mark(slot,"*");return val
        fields=type(obj).arg_types
        if type(fields) is not dict or any(type(k) is not str or type(v) is not bool for k,v in fields.items()):
            mark(slot,"*");return val
        absent=set()
        for key in fields:
            v=args.get(key)
            if isinstance(v,list) and type(v) is not list:
                mark(slot,"*");return val
            if v is None or type(v) is list and not v:
                absent.add(key)
        if len(val & absent)>=2:mark(slot,"*")
        for key in absent:mark(slot,key)
    elif mode=="opaque":mark(slot,"*")
    return val

def dynamic(fn,obj,name,*args):
    if type(name) is str and name in ("arg_types","required_args","__dict__"):
        mark(owner(obj),"*")
    return fn(obj,name,*args)

def variables(fn,obj):
    mark(owner(obj),"*")
    return fn(obj)

def declared(cls):
    if cls.__module__.startswith("sqlglot.expressions.") and "arg_types" in vars(cls):
        sites.add(cls.__module__+":"+cls.__qualname__)
    return cls

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if not name.startswith(("sqlglot.","tests.")) and name not in ("sqlglot","tests"):return None
        spec=importlib.machinery.PathFinder.find_spec(name,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            spec.loader=Loader(name,spec.origin)
        return spec

class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self,module):
        filename=Path(self.path)
        try:rel=str(filename.relative_to(Path(os.environ["BR_ROOT"])))
        except ValueError:rel=""
        imports.append(rel)
        try:return super().exec_module(module)
        finally:imports.pop()

    def get_code(self,name):
        tree=ast.parse(self.get_data(self.path),filename=self.path)
        parents={child:p for p in ast.walk(tree) for child in ast.iter_child_nodes(p)}
        class Rewriter(ast.NodeTransformer):
            def visit_ClassDef(self,node):
                self.generic_visit(node)
                if name.startswith("sqlglot.expressions.") and any(
                    isinstance(st,(ast.Assign,ast.AnnAssign)) and isinstance(st.value,ast.Dict)
                    and any(isinstance(t,ast.Name) and t.id=="arg_types" for t in
                            (st.targets if isinstance(st,ast.Assign) else [st.target])) for st in node.body):
                    node.decorator_list.insert(0,ast.Name("_br_declared",ast.Load()))
                return node
            def visit_Attribute(self,node):
                p=parents.get(node)
                self.generic_visit(node)
                if not isinstance(node.ctx,ast.Load) or node.attr not in ("arg_types","required_args","__dict__"):
                    return node
                mode="opaque"
                if node.attr=="arg_types":
                    # All these consumers observe only the existing keys.
                    if isinstance(p,(ast.For,ast.comprehension)) and p.iter is node:mode="keys"
                    if isinstance(p,ast.Compare) and len(p.ops)==1 and p.comparators[0] is node and isinstance(p.ops[0],(ast.In,ast.NotIn)):mode="keys"
                    if isinstance(p,ast.Call) and isinstance(p.func,ast.Name) and p.func.id in ("len","tuple","zip","set","list"):mode="keys"
                    if name=="sqlglot.expressions.core" and ast.get_source_segment(source,node)=="cls.arg_types":
                        g=parents.get(p)
                        if isinstance(p,ast.Attribute) and p.attr=="items" and isinstance(g,ast.Call):
                            anc=g
                            while anc in parents:
                                anc=parents[anc]
                                if isinstance(anc,ast.FunctionDef):
                                    if anc.name=="__init_subclass__":mode="derived"
                                    break
                if node.attr=="required_args" and isinstance(p,ast.For) and p.iter is node and name=="sqlglot.expressions.core":
                    anc=p
                    while anc in parents:
                        anc=parents[anc]
                        if isinstance(anc,ast.FunctionDef):
                            if anc.name=="error_messages":mode="validation"
                            break
                if mode in ("keys","derived"):return node
                return ast.copy_location(ast.Call(ast.Name("_br_read",ast.Load()),[node.value,ast.Constant(node.attr),ast.Constant(mode)],[]),node)
            def visit_Call(self,node):
                self.generic_visit(node)
                if isinstance(node.func,ast.Name) and not node.keywords:
                    if node.func.id=="getattr" and len(node.args) in (2,3) and not any(isinstance(a,ast.Starred) for a in node.args):
                        if isinstance(node.args[1],ast.Constant) and node.args[1].value not in ("arg_types","required_args","__dict__"):return node
                        return ast.copy_location(ast.Call(ast.Name("_br_dynamic",ast.Load()),[node.func,*node.args],[]),node)
                    if node.func.id=="vars" and len(node.args)==1 and not isinstance(node.args[0],ast.Starred):
                        return ast.copy_location(ast.Call(ast.Name("_br_vars",ast.Load()),[node.func,*node.args],[]),node)
                return node
        source=self.get_data(self.path).decode()
        return compile(ast.fix_missing_locations(Rewriter().visit(tree)),self.path,"exec",dont_inherit=True)

def save():
    data=[sorted(sites),{s:{k:sorted(ids) for k,ids in by_key.items()} for s,by_key in events.items()}]
    (Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".json.gz")).write_bytes(gzip.compress(json.dumps(data).encode(),mtime=0))

if "BR_FLAG_DIR" in os.environ:
    builtins._br_declared=declared;builtins._br_read=view;builtins._br_dynamic=dynamic;builtins._br_vars=variables
    sys.meta_path.insert(0,Finder());atexit.register(save)
