import atexit
import ast
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
from predicates import OPS,OP_NAMES,Probes

registered=set()
reached=set()
comparisons={}
primitive={str,int,float,bool,bytes,type(None)}
numeric={int,float,bool}
def seen(key):
    reached.add(key)
    return False

def relation(a,b):
    # N is unordered. X is unknown. U means equality is defined but ordering
    # is not. Exact builtins keep these probes free of application-level calls.
    x,y=type(a),type(b)
    if x in primitive and y in primitive:
        if x is y or x in numeric and y in numeric:
            if a is None:return "U="
            if a==b:return "="
            if a<b:return "<"
            if a>b:return ">"
            return "N"
        return "U!"
    if (type(a).__eq__ is object.__eq__ and type(b).__eq__ is object.__eq__):
        return "U=" if a is b else "U!"
    return "X"

def compare(key,op,which,a,b):
    record=comparisons.get(key)
    if record is None:record=comparisons[key]=[set(),set()]
    record[0].add((relation(a,b),a is b))
    if which and "*" not in record[1]:
        other=b if which==1 else a
        if type(other) in (int,str,bool) and (not isinstance(other,str) or len(other)<60) and len(record[1])<20:
            record[1].add((type(other).__name__,other))
        elif other is None:record[1].add(("NoneType",None))
        else:record[1]={"*"}
    return OPS[op](a,b)

class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        tree=ast.parse(self.get_data(self.path),filename=self.path)
        rel=str(Path(self.path).relative_to(Path(os.environ["BR_SOURCE"])))
        instrument=Probes(rel)
        tree=instrument.visit(tree)
        registered.update(instrument.sites)
        return compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if not fullname.startswith("sqlglot.") and fullname!="sqlglot":return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
            spec.loader=Loader(fullname,spec.origin)
        return spec

def save():
    data=[list(registered),list(reached),{k:[list(a),list(b)] for k,(a,b) in comparisons.items()}]
    (Path(os.environ["BR_OUT"])/(str(os.getpid())+".gz")).write_bytes(gzip.compress(json.dumps(data).encode(),compresslevel=1,mtime=0))
builtins._br_seen=seen
builtins._br_compare=compare
sys.meta_path.insert(0,Finder())
atexit.register(save)
