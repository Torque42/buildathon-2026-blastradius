"""Tag scalar writes at initialization and reset, then record reads before the next write."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import types

active = os.environ.get("BR_FLAG_TEST", "")
initializing = 0
observations = {}
complete = set()
classes = {"sqlglot.parser": "Parser", "sqlglot.tokenizer_core": "TokenizerCore"}
valid = {}

class Tagged:
    __slots__ = ("site", "value")
    def __init__(self, site, value):
        self.site = site
        self.value = value

class Slot:
    def __init__(self, original):
        self.original = original

    def __get__(self, instance, owner):
        if instance is None:
            return self
        value = self.original.__get__(instance, owner)
        if type(value) is Tagged:
            observations[value.site].add("" if initializing else active)
            return value.value
        return value

    def __set__(self, instance, value):
        self.original.__set__(instance, value)

    def __delete__(self, instance):
        self.original.__delete__(instance)

def tag(instance, site, field, value):
    cls = type(instance)
    key = (cls, field)
    if key not in valid:
        owner = next((base for base in cls.__mro__ if field in vars(base)), None)
        valid[key] = (owner is not None and type(vars(owner)[field]) is Slot
                      and cls.__getattribute__ is object.__getattribute__ and cls.__setattr__ is object.__setattr__)
    if valid[key]:
        return Tagged(site, value)
    observations[site].add("")
    return value

class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        global initializing
        initializing += 1
        try:
            name = module.__name__
            if name not in classes:
                return super().exec_module(module)
            tree = ast.parse(self.get_data(self.path), filename=self.path)
            cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == classes[name])
            fields = set()
            for method in cls.body:
                if not isinstance(method, ast.FunctionDef) or method.name not in ("reset", "__init__"):
                    continue
                for stmt in method.body:
                    if isinstance(stmt, ast.AnnAssign):
                        target = stmt.target
                    elif isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                        target = stmt.targets[0]
                    else:
                        continue
                    if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name) or target.value.id != "self":
                        continue
                    try:
                        literal = ast.literal_eval(stmt.value)
                    except (ValueError, TypeError):
                        continue
                    if type(literal) not in (bool, int, str):
                        continue
                    field = target.attr
                    site = f"{name}:{stmt.lineno}:{field}"
                    fields.add(field)
                    observations.setdefault(site, set())
                    original = stmt.value
                    stmt.value = ast.copy_location(ast.Call(ast.Name("_br_tag_initial",ast.Load()),
                        [ast.Name("self",ast.Load()),ast.Constant(site),ast.Constant(field),original],[]),original)
            exec(compile(ast.fix_missing_locations(tree),self.path,"exec",dont_inherit=True),module.__dict__)
            owner = getattr(module,classes[name])
            for field in fields:
                descriptor = vars(owner)[field]
                if not isinstance(descriptor,types.MemberDescriptorType):
                    raise ValueError((name,field))
                setattr(owner,field,Slot(descriptor))
            complete.add(name)
        finally:
            initializing -= 1

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader=Loader(fullname,spec.origin)
                return spec

def save():
    rows = [(site, ctx, ["read"]) for site, contexts in observations.items() for ctx in contexts]
    rows.extend((site,"$declared",[]) for site in observations)
    blob = json.dumps([sorted(complete),rows],separators=(",",":")).encode()
    (Path(os.environ["BR_FLAG_DIR"])/f"{os.getpid()}.json.gz").write_bytes(gzip.compress(blob,mtime=0))

if "BR_FLAG_DIR" in os.environ:
    builtins._br_tag_initial=tag
    sys.meta_path.insert(0,Finder())
    atexit.register(save)
