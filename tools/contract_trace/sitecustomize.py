"""Trace the actual receiver and key of each class-table read in a pristine query phase."""

import ast
import atexit
import builtins
import enum
import functools
import gzip
import importlib.abc
import importlib.machinery
import marshal
import os
from pathlib import Path
import sys
import types

active = (os.environ["BR_CONTRACT_COLD"], -1, "harness") if "BR_CONTRACT_COLD" in os.environ else None
wrapped = False
rows = {}
classes = {}
descriptors = {}
static_values = {}
missing = object()


def owner(obj):
    if type(obj) is types.ModuleType:
        n = obj.__name__
        return (n, "") if n == "sqlglot" or n.startswith("sqlglot.") else None
    if type(obj) is super:
        return ("*", "super")
    cls = obj if isinstance(obj,type) else type(obj)
    k = id(cls)
    if k not in classes:
        module = type.__getattribute__(cls,"__module__")
        classes[k] = cls, ((module,type.__getattribute__(cls,"__qualname__"))
                           if module == "sqlglot" or module.startswith("sqlglot.") else None)
    return classes[k][1]


def remember(key):
    table = rows.get(active)
    if table is None:
        table = rows[active] = set()
    idx = descriptors.get(key)
    if idx is None:
        idx = descriptors[key] = len(descriptors)
    table.add(idx)


def observe(obj, name):
    if active and active[0] != "init" and type(name) is str:
        target = owner(obj)
        if target:
            remember((*target, name, "", None))
    return obj


def encoding(k):
    if type(k) in (str, int, bool, type(None)):
        return ("literal",k) if type(k) is not str or len(k)<240 else None
    if isinstance(k,enum.Enum):
        cls=type(k)
        return ("enum",cls.__module__,cls.__qualname__,k.name)
    if isinstance(k,type):
        return ("type",type.__getattribute__(k,"__module__"),type.__getattribute__(k,"__qualname__"))


def record(obj, name, table, key, mode):
    if not active or active[0]=="init":
        return
    who=owner(obj)
    if not who:
        return
    cls=obj if isinstance(obj,type) else type(obj)
    lookup=(id(obj) if type(obj) is types.ModuleType else id(cls),name)
    if lookup not in static_values:
        if type(obj) is types.ModuleType:
            value=vars(obj).get(name,missing)
        else:
            value=next((type.__getattribute__(base,"__dict__")[name]
                        for base in type.__getattribute__(cls,"__mro__")
                        if name in type.__getattribute__(base,"__dict__")),missing)
        static_values[lookup]=value
    encoded=encoding(key) if static_values[lookup] is table and type(table) in (dict,set,frozenset) else None
    remember((*who,name,mode if encoded is not None else "",encoded))


def method(obj, attr):
    table=getattr(obj,attr)
    fn=table.get
    def get(key,*default):
        record(obj,attr,table,key,"lookup")
        return fn(key,*default)
    return get


def subscript(obj, attr):
    table=getattr(obj,attr)
    def get(key):
        record(obj,attr,table,key,"lookup")
        return table[key]
    return get


def contains(key,obj,attr,negative):
    table=getattr(obj,attr)
    record(obj,attr,table,key,"member")
    return key not in table if negative else key in table


def lookup(obj,name,*default):
    return getattr(observe(obj,name),name,*default)


def has(obj,name):
    return hasattr(observe(obj,name),name)


class Instrument(ast.NodeTransformer):
    def call(self, node, fn, *args):
        return ast.copy_location(ast.Call(ast.Name("_br_contract_"+fn,ast.Load()),list(args),[]),node)

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if isinstance(node.ctx, ast.Load):
            node.value=self.call(node.value,"observe",node.value,ast.Constant(node.attr))
        return node

    def visit_Call(self,node):
        f=node.func
        if (isinstance(f,ast.Attribute) and f.attr=="get" and isinstance(f.value,ast.Attribute)
                and not node.keywords and len(node.args) in (1,2)
                and not any(isinstance(a,ast.Starred) for a in node.args)):
            ref=f.value
            node.func=self.call(f,"method",self.visit(ref.value),ast.Constant(ref.attr))
            node.args=[self.visit(a) for a in node.args]
            return node
        self.generic_visit(node)
        if isinstance(node.func,ast.Name) and node.func.id in {"getattr","hasattr"}:
            node.func.id="_br_contract_"+("lookup" if node.func.id=="getattr" else "has")
        return node

    def visit_Subscript(self,node):
        ref=node.value
        if isinstance(node.ctx,ast.Load) and isinstance(ref,ast.Attribute) and not isinstance(node.slice,(ast.Slice,ast.Tuple)):
            fn=self.call(ref,"subscript",self.visit(ref.value),ast.Constant(ref.attr))
            return ast.copy_location(ast.Call(fn,[self.visit(node.slice)],[]),node)
        return self.generic_visit(node)

    def visit_Compare(self,node):
        if (len(node.ops)==1 and isinstance(node.ops[0],(ast.In,ast.NotIn))
                and isinstance(node.comparators[0],ast.Attribute)):
            ref=node.comparators[0]
            return self.call(node,"contains",self.visit(node.left),self.visit(ref.value),ast.Constant(ref.attr),
                             ast.Constant(isinstance(node.ops[0],ast.NotIn)))
        return self.generic_visit(node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        tree=ast.parse(Path(self.path).read_bytes(),self.path)
        tree=ast.fix_missing_locations(Instrument().visit(tree))
        return compile(tree,self.path,"exec",dont_inherit=True)
    def exec_module(self,module):
        global wrapped
        super().exec_module(module)
        if "BR_CONTRACT_COLD" not in os.environ or module.__name__!="tests.test_executor":
            return
        def exclude(fn,caller):
            @functools.wraps(fn)
            def call(*args,**kwargs):
                global active
                if caller is not None and sys._getframe(1).f_code.co_name!=caller:
                    return fn(*args,**kwargs)
                prev,active=active,None
                try:
                    return fn(*args,**kwargs)
                finally:
                    active=prev
            return call
        module.parse_one=exclude(module.parse_one,"_mp_execute")
        module.transpile=exclude(module.transpile,"cached_execute")
        module.mp_execute=exclude(module.mp_execute,None)
        wrapped=True


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path,target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot.") or fullname=="tests.test_executor":
            spec=importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and spec.origin and spec.origin.endswith(".py"):
                spec.loader=Loader(fullname,spec.origin)
                return spec


if "BR_CONTRACT_OUT" in os.environ:
    for fn in (observe,method,subscript,contains,lookup,has):
        setattr(builtins,"_br_contract_"+fn.__name__,fn)
    sys.meta_path.insert(0,Finder())
    def save():
        out=Path(os.environ["BR_CONTRACT_OUT"])
        if "BR_CONTRACT_COLD" in os.environ:
            if not wrapped:
                return
            out=out/(str(os.getpid())+".gz")
        out.write_bytes(gzip.compress(marshal.dumps((sys.version_info[:2],list(descriptors),
                                                     {k:sorted(v) for k,v in rows.items()})),mtime=0))
    atexit.register(save)
