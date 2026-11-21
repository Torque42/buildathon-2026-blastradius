import ast,atexit,builtins,gzip,importlib.abc,importlib.machinery,json,os,sys
from pathlib import Path
from enum import Enum
from functools import lru_cache
active=os.environ.get("BR_TEST","")
seen={};sites=set()
primitive={type(None),bool,int,float,str,bytes,dict,list,tuple,set,frozenset,range,type}
@lru_cache(2048)
def kind(cls):
    eq=cls.__eq__;ne=cls.__ne__
    if cls in primitive:return "builtin"
    if eq is object.__eq__ and ne is object.__ne__:return "object"
    if (getattr(eq,"__module__","")=="sqlglot.expressions.core" and getattr(eq,"__qualname__","")=="Expr.__eq__"
        and getattr(ne,"__module__","")=="sqlglot.expressions.core" and getattr(ne,"__qualname__","")=="Expr.__ne__"):
        return "guarded-expression"
    if issubclass(cls,Enum) and eq in (str.__eq__,int.__eq__) and ne in (str.__ne__,int.__ne__):return "scalar-enum"
    return "custom"

def observe(key,left,right,negate):
    a,b=type(left),type(right)
    relation=(kind(a),kind(b),a is b,left is None or right is None,left is right)
    seen.setdefault(key,{}).setdefault(relation,set()).add(active)
    return (left is not right) if negate else (left is right)

class Instrument(ast.NodeTransformer):
    def __init__(self,path):self.path=path
    def visit_Compare(self,node):
        self.generic_visit(node)
        if len(node.ops)!=1 or not isinstance(node.ops[0],(ast.Is,ast.IsNot)):return node
        key=f"{self.path}:{node.lineno}:{node.col_offset}"
        sites.add(key)
        return ast.copy_location(ast.Call(ast.Name("_br_id_observe",ast.Load()),[ast.Constant(key),node.left,node.comparators[0],ast.Constant(isinstance(node.ops[0],ast.IsNot))],[]),node)
class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        name=str(Path(self.path).relative_to(Path(os.environ["BR_SOURCE"])))
        tree=Instrument(name).visit(ast.parse(self.get_data(self.path),filename=self.path))
        return compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if not fullname.startswith("sqlglot.") and fullname!="sqlglot":return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):spec.loader=Loader(fullname,spec.origin)
        return spec
def save():
    payload=[list(sites),{k:[[key,sorted(v)] for key,v in rows.items()] for k,rows in seen.items()}]
    (Path(os.environ["BR_OUT"])/(str(os.getpid())+".json.gz")).write_bytes(gzip.compress(json.dumps(payload).encode(),mtime=0))
builtins._br_id_observe=observe;sys.meta_path.insert(0,Finder());atexit.register(save)
