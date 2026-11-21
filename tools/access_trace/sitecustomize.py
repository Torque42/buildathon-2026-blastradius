import ast,atexit,builtins,gzip,importlib.abc,importlib.machinery,marshal,os,sys
from pathlib import Path
from access_sites import recognize

active=os.environ.get("BR_FLAG_TEST","")
observations={}
def record(value,site,mode,fn=None):
    row=observations.setdefault(site,{}).setdefault(active,set())
    if None in row:return value
    if mode in ("get","get-default"):
        if type(value) is not dict or len(value)>40 or any(type(k) is not str for k in value):
            row.clear();row.add(None)
        else:
            row.update(k for k,v in value.items() if mode=="get-default" or v is not None)
            if len(row)>96:row.clear();row.add(None)
    elif type(value) not in (list,tuple) or len(value)>24 or (mode=="seq" and (fn is not getattr(sys.modules.get("sqlglot.helper"),"seq_get",None) or getattr(fn,"__module__","")!="sqlglot.helper" or getattr(fn,"__qualname__","")!="seq_get")):
        row.clear();row.add(None)
    else:
        aliases={id(None):0}
        shape=tuple(aliases.setdefault(id(v),i+1) for i,v in enumerate(value))
        row.add(shape)
        if len(row)>36:row.clear();row.add(None)
    return value
class Rewrite(ast.NodeTransformer):
    def __init__(self,path):self.path=path
    def generic_visit(self,node):
        item=recognize(node)
        super().generic_visit(node)
        if not item:return node
        mode,_,arg,_=item
        if mode=="seq":value=node.args[0]
        elif mode=="index":value=node.value
        else:value=node.func.value
        args=[value,ast.Constant((self.path,arg.lineno,arg.col_offset)),ast.Constant(mode)]
        if mode=="seq":args.append(ast.Name("seq_get",ast.Load()))
        wrapped=ast.copy_location(ast.Call(ast.Name("_br_access_read",ast.Load()),args,[]),value)
        if mode=="seq":node.args[0]=wrapped
        elif mode=="index":node.value=wrapped
        else:node.func.value=wrapped
        return node
class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,name):
        path="sqlglot/"+self.path.split("/sqlglot/",1)[1]
        tr=Rewrite(path).visit(ast.parse(self.get_data(self.path),filename=self.path))
        return compile(ast.fix_missing_locations(tr),self.path,"exec",dont_inherit=True)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name!="sqlglot" and not name.startswith("sqlglot."):return None
        spec=importlib.machinery.PathFinder.find_spec(name,path)
        if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):spec.loader=Loader(name,spec.origin)
        return spec
def save():
    out=Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".rec")
    out.write_bytes(gzip.compress(marshal.dumps(observations),compresslevel=1,mtime=0))
builtins._br_access_read=record
sys.meta_path.insert(0,Finder())
atexit.register(save)
