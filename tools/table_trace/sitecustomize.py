"""Observe parser table reads, including lookups that miss, on the unmodified test suite."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
import sys
from enum import Enum
from pathlib import Path

TABLES = {"FUNCTIONS", "FUNCTION_PARSERS", "NO_PAREN_FUNCTION_PARSERS"}
dispatch_mode = os.environ.get("BR_DIALECT_TABLES") == "1"
if dispatch_mode:
    TABLES |= set(("TRANSFORMS TYPE_MAPPING PROPERTIES_LOCATION JSON_PATH_PART_TRANSFORMS "
                  "STATEMENT_PARSERS EXPRESSION_PARSERS PROPERTY_PARSERS PRIMARY_PARSERS "
                  "STRING_PARSERS NUMERIC_PARSERS RANGE_PARSERS QUERY_MODIFIER_PARSERS "
                  "CONSTRAINT_PARSERS ALTER_PARSERS ALTER_ALTER_PARSERS SET_PARSERS SHOW_PARSERS "
                  "TYPE_CONVERTERS TYPE_LITERAL_PARSERS LAMBDAS COLUMN_OPERATORS JSON_OPERATORS "
                  "EQUALITY COMPARISON BITWISE TERM FACTOR EXPONENT _dispatch").split())
active = os.environ.get("BR_FLAG_TEST","")
imports = []
reads = {}
sites = set()
seen = {}

def owner(obj, name):
    if dispatch_mode and name=="_dispatch":
        name="TRANSFORMS"
    cls = obj if isinstance(obj,type) else type(obj)
    ck = (cls,name)
    if ck not in seen:
        result = None
        for base in cls.__mro__:
            if name in vars(base):
                if base.__module__.startswith("sqlglot.") and type(vars(base)[name]) is dict:
                    result = base.__module__+":"+base.__qualname__+":"+name
                break
        seen[ck] = result
    return seen[ck]

def mark(site, key):
    if not site:
        return
    if dispatch_mode and isinstance(key,type) and key.__module__.startswith("sqlglot.expressions"):
        key="expr:"+key.__name__
    elif dispatch_mode and isinstance(key,Enum):
        key="enum:"+key.name
    elif dispatch_mode and type(key) is str and key!="*":
        key="string:"+key
    elif type(key) is not str:
        key="*"
    context = active
    if imports:
        file = imports[-1]
        context = ("import:" if file.startswith("sqlglot/") else "test-import:")+file
    reads.setdefault(site,{}).setdefault(key,set()).add(context)

def visit(obj,name,key):
    mark(owner(obj,name),key)
    return key

def copied(obj, name, target):
    slot = owner(obj,name)
    if slot:
        reads.setdefault(slot,{}).setdefault("@copy",set()).add(target)
    return getattr(obj,name)

def opaque(obj,name):
    original = getattr(obj,name)
    if type(original) is dict:
        mark(owner(obj,name),"*")
    return original

def declared(cls):
    for name in TABLES:
        if name in vars(cls) and type(vars(cls)[name]) is dict:
            sites.add(cls.__module__+":"+cls.__qualname__+":"+name)
    return cls

def dynamic(fn,obj,name,*extra):
    if type(name) is str and (name in TABLES or name=="__dict__"):
        for table in (TABLES if name=="__dict__" else [name]):
            mark(owner(obj,table),"*")
    return fn(obj,name,*extra)

def variables(fn,obj):
    for table in TABLES:
        mark(owner(obj,table),"*")
    return fn(obj)

def attr_table(n):
    base=getattr(n,"value",None)
    if dispatch_mode:
        while isinstance(base,ast.Attribute):
            base=base.value
    return isinstance(n,ast.Attribute) and n.attr in TABLES and isinstance(base,ast.Name) and isinstance(n.ctx,ast.Load)

def enclosing(node,parents,kind):
    while node in parents:
        node = parents[node]
        if isinstance(node,kind):
            return node
    return None

def single_copy(node,parents):
    # In a direct class table spread the keys keep their names. Track all dialects'
    # keyed observations at selection time. Other copying and iteration are opaque.
    spread = node
    outer = parents.get(node)
    # A string-key filter of the form {k:v for k,v in source.items() if k!=s}
    # preserves both keys and values for every retained entry. The selector
    # conservatively follows the edge even for the excluded string.
    if isinstance(outer,ast.Attribute) and outer.attr=="items":
        call=parents.get(outer)
        comp=parents.get(call)
        table=parents.get(comp)
        if (not isinstance(call,ast.Call) or call.args or call.keywords or not isinstance(comp,ast.comprehension)
                or not isinstance(table,ast.DictComp) or table.generators!=[comp] or comp.is_async
                or not isinstance(comp.target,ast.Tuple) or len(comp.target.elts)!=2):
            return False
        keys=comp.target.elts
        if not all(isinstance(n,ast.Name) for n in (*keys,table.key,table.value)):
            return False
        if [table.key.id,table.value.id]!=[n.id for n in keys]:
            return False
        if any(not isinstance(n,ast.Compare) or not isinstance(n.left,ast.Name) or n.left.id!=keys[0].id
               or len(n.ops)!=1 or not isinstance(n.ops[0],(ast.Eq,ast.NotEq)) or len(n.comparators)!=1
               or not isinstance(n.comparators[0],ast.Constant) or type(n.comparators[0].value) is not str
               for n in comp.ifs):
            return False
        spread=table
        outer=parents.get(table)
    if not isinstance(outer,ast.Dict) or not any(key is None and val is spread for key,val in zip(outer.keys,outer.values)):
        return False
    assign = parents.get(outer)
    if not isinstance(assign,(ast.Assign,ast.AnnAssign)) or not isinstance(parents.get(assign),ast.ClassDef):
        return False
    targets=assign.targets if isinstance(assign,ast.Assign) else [assign.target]
    return len(targets)==1 and isinstance(targets[0],ast.Name) and targets[0].id==node.attr

class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self,module):
        rel=str(Path(self.path).relative_to(os.environ["BR_ROOT"]))
        imports.append(rel)
        try: super().exec_module(module)
        finally: imports.pop()

    def get_code(self,name):
        tree=ast.parse(self.get_data(self.path),filename=self.path)
        parents={child:n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)}
        def call(fn,*values):
            return ast.Call(ast.Name(fn,ast.Load()),list(values),[])
        class Rewriter(ast.NodeTransformer):
            def visit_ClassDef(self,node):
                self.generic_visit(node)
                if name.startswith("sqlglot.") and any(
                    isinstance(st,(ast.Assign,ast.AnnAssign)) and any(isinstance(t,ast.Name) and t.id in TABLES for t in
                        (st.targets if isinstance(st,ast.Assign) else [st.target])) for st in node.body):
                    node.decorator_list.insert(0,ast.Name("_br_table_declared",ast.Load()))
                return node
            def visit_Attribute(self,node):
                p=parents.get(node)
                if not isinstance(node.ctx,ast.Load) or node.attr not in TABLES:
                    return self.generic_visit(node)
                if dispatch_mode and name=="sqlglot.generator" and node.attr=="TRANSFORMS":
                    fn=enclosing(node,parents,ast.FunctionDef)
                    if (fn is not None and fn.name=="_build_dispatch" and isinstance(p,ast.Call) and
                            isinstance(p.func,ast.Name) and p.func.id=="dict" and p.args==[node]):
                        # Generator.sql consumes the completed dictionary through self._dispatch.
                        return node
                if single_copy(node,parents):
                    cls=enclosing(node,parents,ast.ClassDef)
                    if cls is not None:
                        containing=[]
                        parent=cls
                        while parent is not None:
                            if isinstance(parent,ast.ClassDef):
                                containing.append(parent.name)
                            parent=parents.get(parent)
                        target=name+":"+".".join(reversed(containing))+":"+node.attr
                        return ast.copy_location(call("_br_table_copy",self.visit(node.value),ast.Constant(node.attr),ast.Constant(target)),node)
                if attr_table(node):
                    key = None
                    g=parents.get(p)
                    if isinstance(p,ast.Subscript) and p.value is node:
                        key=p.slice
                    elif isinstance(p,ast.Attribute) and p.attr=="get" and isinstance(g,ast.Call) and g.func is p and g.args:
                        key=g.args[0]
                    elif isinstance(p,ast.Compare) and len(p.ops)==1 and p.comparators[0] is node and isinstance(p.ops[0],(ast.In,ast.NotIn)):
                        # The left side has already been transformed. Re-evaluate only the plain owner name.
                        key=p.left
                    if key is not None and not isinstance(key,(ast.Starred,ast.Slice)):
                        tracked=ast.copy_location(call("_br_table_key",node.value,ast.Constant(node.attr),key),key)
                        if isinstance(p,ast.Subscript):p.slice=tracked
                        elif isinstance(p,ast.Compare):p.left=tracked
                        else:g.args[0]=tracked
                        return node
                    if (name=="sqlglot.parser" and node.attr=="FUNCTIONS" and isinstance(p,ast.Assign) and
                        isinstance(p.targets[0],ast.Name) and p.targets[0].id=="functions" and
                        enclosing(node,parents,ast.FunctionDef).name=="_parse_function_call"):
                        return node
                return ast.copy_location(call("_br_table_opaque",self.visit(node.value),ast.Constant(node.attr)),node)
            def visit_Call(self,node):
                if (name=="sqlglot.parser" and isinstance(node.func,ast.Attribute) and node.func.attr=="get" and
                    isinstance(node.func.value,ast.Name) and node.func.value.id=="functions" and len(node.args)==1 and
                    enclosing(node,parents,ast.FunctionDef).name=="_parse_function_call"):
                    node.args[0] = ast.copy_location(call("_br_table_key",ast.Name("self",ast.Load()),ast.Constant("FUNCTIONS"),node.args[0]),node.args[0])
                    return node
                self.generic_visit(node)
                if isinstance(node.func,ast.Name) and not node.keywords and not any(isinstance(v,ast.Starred) for v in node.args):
                    if node.func.id=="vars" and len(node.args)==1:
                        return ast.copy_location(call("_br_table_vars",node.func,*node.args),node)
                    if node.func.id=="getattr" and len(node.args) in (2,3):
                        key=node.args[1]
                        if not isinstance(key,ast.Constant) or key.value in TABLES or key.value=="__dict__":
                            return ast.copy_location(call("_br_table_dynamic",node.func,*node.args),node)
                return node
        return compile(ast.fix_missing_locations(Rewriter().visit(tree)),self.path,"exec",dont_inherit=True)

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name not in ("sqlglot","tests") and not name.startswith(("sqlglot.","tests.")):
            return None
        spec=importlib.machinery.PathFinder.find_spec(name,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            spec.loader=Loader(name,spec.origin)
        return spec

def save():
    events={site:{k:sorted(v) for k,v in rows.items()} for site,rows in reads.items()}
    out=Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".json.gz")
    out.write_bytes(gzip.compress(json.dumps([sorted(sites),events]).encode(),mtime=0))

builtins._br_table_copy=copied
builtins._br_table_declared=declared
builtins._br_table_key=visit
builtins._br_table_opaque=opaque
builtins._br_table_vars=variables
builtins._br_table_dynamic=dynamic
sys.meta_path.insert(0,Finder())
atexit.register(save)
