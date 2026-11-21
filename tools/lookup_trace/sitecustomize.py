"""Observe which alternate literal keys would preserve each builtin dict.get result."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import marshal
import os
from pathlib import Path
import sys
import types

active = os.environ.get("BR_FLAG_TEST","")
loading = 0
states = {}
sites = set()


def lookup(site, fn, key, *args):
    result = fn(key, *args)
    context = "" if loading else active
    state = states.setdefault((site,context), [False, False, set(), None])
    if state[0]:
        return result
    table = getattr(fn, "__self__", None) if isinstance(fn,types.BuiltinMethodType) else None
    if type(table) is not dict or len(table) > 100 or any(type(k) is not str for k in table):
        state[0] = True
        return result
    if key not in table:
        state[1] = True
    if state[3] is not None and len(state[3]) <= 1 and key in state[3]:
        return result
    default = args[0] if args else None
    if result is default:
        state[2].update(k for k,v in table.items() if v is not result)
        if state[3] is not None:
            state[3].difference_update(state[2])
    else:
        equivalent = {k for k, v in table.items() if v is result} - state[2]
        if state[3] is None:
            state[3] = equivalent
        else:
            state[3].intersection_update(equivalent)
    return result


class Rewrite(ast.NodeTransformer):
    def __init__(self,path):
        self.path = path

    def visit_Call(self,node):
        self.generic_visit(node)
        if (not isinstance(node.func,ast.Attribute) or node.func.attr!="get" or node.keywords
                or len(node.args) not in (1,2) or not isinstance(node.args[0],ast.Constant)
                or type(node.args[0].value) is not str or any(isinstance(arg,ast.Starred) for arg in node.args)):
            return node
        site = (self.path,node.lineno,node.col_offset)
        sites.add(site)
        return ast.copy_location(ast.Call(ast.Name("_br_observed_get",ast.Load()),[
            ast.Constant(site),node.func,*node.args],[]),node)


class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self,fullname):
        source = self.get_data(self.path)
        tree = ast.parse(source,filename=self.path)
        name = str(Path(self.path).relative_to(os.environ["BR_LOOKUP_PIN"]))
        return compile(ast.fix_missing_locations(Rewrite(name).visit(tree)),self.path,"exec",dont_inherit=True)

    def exec_module(self,module):
        global loading
        loading += 1
        try:
            super().exec_module(module)
        finally:
            loading -= 1


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader=Loader(fullname,spec.origin)
                return spec


def save():
    portable = {}
    for (site,context), (unknown,missing,bad,good) in states.items():
        portable[(site,context)] = (unknown,missing,tuple(sorted(bad)) if good is None and not unknown else (),
                                    None if good is None else tuple(sorted(good)))
    target = Path(os.environ["BR_FLAG_DIR"])/f"{os.getpid()}.bin.gz"
    target.write_bytes(gzip.compress(marshal.dumps((sites,portable)),mtime=0))


if "BR_FLAG_DIR" in os.environ:
    builtins._br_observed_get = lookup
    sys.meta_path.insert(0, Finder())
    atexit.register(save)
