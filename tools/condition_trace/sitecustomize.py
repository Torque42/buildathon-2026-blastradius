import ast,atexit,builtins,gzip,importlib.abc,importlib.machinery,json,os,sys,types
from pathlib import Path
from functools import lru_cache
from conditions import compile_condition
registered={}
states={}
unavailable=object()
atoms={int,bool,str,bytes,float,type(None)}
containers={tuple,list,dict,set,frozenset,str,bytes}
numeric={int,float,bool}

@lru_cache(maxsize=8192)
def descriptor(cls,name):
    for typ in type.__getattribute__(cls,"__mro__"):
        attrs=type.__getattribute__(typ,"__dict__")
        if name in attrs:return attrs[name]
    return unavailable

def value(spec,frame):
    kind,x,*more=spec
    if kind=="literal":return x
    if kind=="name":
        lookup=frame.f_locals
        if x in lookup:return lookup[x]
        return frame.f_globals.get(x,unavailable)
    obj=value(x,frame)
    if obj is unavailable:return obj
    if kind=="len":
        if frame.f_globals.get("len",len) is len and frame.f_locals.get("len",len) is len and type(obj) in containers:
            return len(obj)
        return unavailable
    name=more[0]
    if kind=="get":
        if type(obj) is dict and type(name) in (str,int) and all(type(k) in (str,int) for k in obj):
            return obj.get(name)
        return unavailable
    if type(obj).__getattribute__ is not object.__getattribute__:return unavailable
    desc=descriptor(type(obj),name)
    if isinstance(desc,types.MemberDescriptorType):
        try:return desc.__get__(obj,type(obj))
        except AttributeError:return unavailable
    if desc is not unavailable and hasattr(type(desc),"__get__"):return unavailable
    try:return object.__getattribute__(obj,"__dict__").get(name,desc)
    except AttributeError:return desc

def leaf(spec,frame):
    a=value(spec[1],frame)
    if a is unavailable:return "?"
    if spec[0]=="truth":
        if type(a) in atoms|containers:return "T" if bool(a) else "F"
        return "?"
    b=value(spec[2],frame)
    if b is unavailable:return "?"
    x,y=type(a),type(b)
    if x not in atoms or y not in atoms:return "?"
    identity="1" if a is b else "0"
    if (x is y or x in numeric and y in numeric) and a is not None:
        return identity+("=" if a==b else "<" if a<b else ">" if a>b else "N")
    return identity+("U" if a==b else "D")

def visit(key):
    bucket=states.setdefault(key,set())
    if None in bucket:return False
    if len(bucket)>50:
        bucket.clear();bucket.add(None);return False
    frame=sys._getframe(1)
    try:sample=tuple(leaf(spec,frame) for spec in registered[key])
    except Exception:sample=None
    # A partially unknown input cannot certify the absence of side effects.
    if sample is None or "?" in sample:
        bucket.clear();bucket.add(None)
    else:bucket.add(sample)
    return False

class Transformer(ast.NodeTransformer):
    def __init__(self,path):self.path=path;self.inside=False
    def visit_FunctionDef(self,node):
        old=self.inside;self.inside=True
        node.body=[self.visit(s) for s in node.body]
        self.inside=old
        return node
    visit_AsyncFunctionDef=visit_FunctionDef
    def visit_If(self,node):
        old=node.test
        condition=compile_condition(old) if self.inside else None
        self.generic_visit(node)
        if condition:
            key=f"{self.path}:{old.lineno}:{old.col_offset}"
            registered[key]=condition[0]
            node.test=ast.copy_location(ast.BoolOp(ast.Or(),[ast.Call(ast.Name("_br_cond",ast.Load()),[ast.Constant(key)],[]),node.test]),old)
        return node
    visit_IfExp=visit_If
    visit_While=visit_If

class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        path=str(Path(self.path).relative_to(Path(os.environ["BR_SOURCE"])))
        tree=Transformer(path).visit(ast.parse(self.get_data(self.path),filename=self.path))
        return compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if not fullname.startswith("sqlglot.") and fullname!="sqlglot":return None
        spec=importlib.machinery.PathFinder.find_spec(fullname,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):spec.loader=Loader(fullname,spec.origin)
        return spec
def save():
    data=[list(registered),{k:list(v) for k,v in states.items()}]
    (Path(os.environ["BR_OUT"])/(str(os.getpid())+".gz")).write_bytes(gzip.compress(json.dumps(data).encode(),compresslevel=1))
builtins._br_cond=visit
sys.meta_path.insert(0,Finder())
atexit.register(save)
