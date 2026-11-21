"""Track reads of literal values in simple module- and class-level dicts."""
import ast,builtins,importlib.abc,importlib.machinery,sys
from collections import defaultdict
from types import ModuleType
active=None
import_depth=0
value_reads=defaultdict(lambda:defaultdict(set))
created=set()


class Watched(dict):
    def __init__(self,path,values):
        super().__init__(values)
        self.path=path
        created.add(path)

    def read(self,key):
        value_reads[self.path][None if import_depth else active].add(key if isinstance(key,str) else None)

    def __getitem__(self,key):
        if key in self:self.read(key)
        return super().__getitem__(key)
    def get(self,key,*default):
        if key in self:self.read(key)
        return super().get(key,*default)
    def items(self): self.read(None);return super().items()
    def values(self): self.read(None);return super().values()
    def copy(self): self.read(None);return super().copy()
    def __repr__(self): self.read(None);return super().__repr__()
    def __eq__(self,other): self.read(None);return super().__eq__(other)
    def __ne__(self,other): self.read(None);return super().__ne__(other)
    def __or__(self,other): self.read(None);return super().__or__(other)
    def __ror__(self,other): self.read(None);return super().__ror__(other)
    def __iter__(self): return super().__iter__()
    def pop(self,key,*args): self.read(key);return super().pop(key,*args)
    def popitem(self): self.read(None);return super().popitem()
    def setdefault(self,key,*args):
        if key in self:self.read(key)
        return super().setdefault(key,*args)


def transform(module,owner,body):
    for stmt in body:
        if isinstance(stmt,ast.ClassDef):
            transform(module,owner+[stmt.name],stmt.body)
        elif isinstance(stmt,(ast.Assign,ast.AnnAssign)) and isinstance(stmt.value,ast.Dict):
            targets=stmt.targets if isinstance(stmt,ast.Assign) else [stmt.target]
            if len(targets)!=1 or not isinstance(targets[0],ast.Name) or not stmt.value.keys:continue
            if not all(isinstance(k,ast.Constant) and isinstance(k.value,str) for k in stmt.value.keys):continue
            try: ast.literal_eval(stmt.value)
            except (TypeError,ValueError):continue
            key=module+':'+'.'.join(owner)+':'+targets[0].id
            call=ast.Call(func=ast.Name(id='__br_watch_values',ctx=ast.Load()),args=[ast.Constant(key),stmt.value],keywords=[])
            call.func.id='_br_watch_values'
            stmt.value=ast.copy_location(call,stmt.value)

class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self,module):
        global import_depth
        import_depth+=1
        try:
            return super().exec_module(module)
        finally:
            import_depth-=1

    def get_code(self,fullname):
        tree=ast.parse(self.get_data(self.path),filename=self.path)
        transform(fullname,[],tree.body)
        ast.fix_missing_locations(tree)
        return compile(tree,self.path,"exec",dont_inherit=True)
class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=="sqlglot" or fullname.startswith("sqlglot."):
            spec=importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader=Loader(fullname,spec.origin)
            return spec
def install():
    builtins._br_watch_values=Watched
    sys.meta_path.insert(0,Finder())
