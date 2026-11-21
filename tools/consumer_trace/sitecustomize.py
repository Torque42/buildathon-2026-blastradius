"""Measure reads of unmodified lexer keys and slot values during real pytest execution."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
import sys
import types
from pathlib import Path

active = os.environ.get("BR_FLAG_TEST","")
plan = json.loads(Path(os.environ["BR_CONSUMER_PLAN"]).read_bytes())
contexts = {}
seen_slots = set()
registered_keywords = False

def mark(name):
    contexts.setdefault(name,set()).add(active)

class Words:
    def __init__(self, data):
        self.data=data
    def __contains__(self,key):
        mark("key:"+key)
        return key in self.data
    def __getitem__(self,key):
        mark("key:"+key)
        return self.data[key]
    def get(self,key,default=None):
        mark("key:"+key)
        return self.data.get(key, default)

class Slot:
    def __init__(self, original, site):
        self.member=original
        self.site=site
    def __get__(self,obj,cls=None):
        mark(self.site)
        return self.member.__get__(obj,cls)
    def __set__(self,obj,value):
        return self.member.__set__(obj,value)
    def __delete__(self,obj):
        return self.member.__delete__(obj)

def instrument(cls,path):
    for field in plan["slots"].get(path,{}).get(cls.__name__,[]):
        site=path+":"+cls.__name__+":"+field
        member=vars(cls).get(field)
        if isinstance(member,types.MemberDescriptorType):
            setattr(cls,field,Slot(member,site))
            seen_slots.add(site)
    return cls

def check_store(obj, field, site):
    # A subclass property or custom setter can observe the input during assignment.
    if type(obj).__setattr__ is not object.__setattr__:
        mark(site)
    else:
        for cls in type(obj).__mro__:
            v=vars(cls).get(field)
            if v is not None:
                if not isinstance(v,Slot) or v.site!=site:
                    mark(site)
                break
        else:
            mark(site)
    return obj

class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,name):
        global registered_keywords
        raw=self.get_data(self.path)
        tree=ast.parse(raw,filename=self.path)
        path="sqlglot/"+self.path.split("/sqlglot/",1)[1]
        class Probe(ast.NodeTransformer):
            def visit_ClassDef(self,node):
                self.generic_visit(node)
                if node.name in plan["slots"].get(path,{}):
                    deco=ast.Lambda(ast.arguments([], [ast.arg("_cls")], None,[],[],None,[]),
                        ast.Call(ast.Name("_br_install_consumers",ast.Load()),[ast.Name("_cls",ast.Load()),ast.Constant(path)],[]))
                    node.decorator_list.append(deco)
                    fields=plan["slots"][path][node.name]
                    for fn in node.body:
                        if not isinstance(fn,ast.FunctionDef):continue
                        for op in ast.walk(fn):
                            if isinstance(op,ast.Attribute) and isinstance(op.ctx,ast.Store) and op.attr in fields and isinstance(op.value,ast.Name) and op.value.id=="self":
                                op.value=ast.copy_location(ast.Call(ast.Name("_br_check_store",ast.Load()),[op.value,ast.Constant(op.attr),ast.Constant(path+":"+node.name+":"+op.attr)],[]),op.value)
                return node
            def visit_Assign(self,node):
                global registered_keywords
                if name=="sqlglot.tokenizer_core" and len(node.targets)==1 and isinstance(node.targets[0],ast.Attribute) and node.targets[0].attr=="keywords" and isinstance(node.value,ast.Name) and node.value.id=="keywords":
                    node.value=ast.copy_location(ast.Call(ast.Name("_br_words",ast.Load()),[node.value],[]),node.value)
                    registered_keywords=True
                return node
        return compile(ast.fix_missing_locations(Probe().visit(tree)),self.path,"exec",dont_inherit=True)

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if not name.startswith("sqlglot."):return None
        key=name.replace(".","/")+".py"
        if name!="sqlglot.tokenizer_core" and key not in plan["slots"]:return None
        spec=importlib.machinery.PathFinder.find_spec(name,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            spec.loader=Loader(name,spec.origin)
        return spec

def save():
    out=Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".json.gz")
    out.write_bytes(gzip.compress(json.dumps([sorted(seen_slots),registered_keywords,{k:sorted(v) for k,v in contexts.items()}]).encode(),compresslevel=1,mtime=0))

builtins._br_install_consumers=instrument
builtins._br_check_store=check_store
builtins._br_words=Words
sys.meta_path.insert(0,Finder())
atexit.register(save)
