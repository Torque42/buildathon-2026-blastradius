"""Collect owning class and module of each dynamic attribute read."""
import ast
import builtins
from collections import defaultdict
import importlib.abc
import importlib.machinery
import json
import sys
from types import ModuleType

active = None
seen = set()
observed = defaultdict(set)
import_reads = defaultdict(set)
slots = {}
PENDING = 0


def owner(cls, name):
    key = cls, name
    if key not in slots:
        slots[key] = next((
            (type.__getattribute__(typ, "__module__"), type.__getattribute__(typ, "__qualname__"), name)
            for typ in type.__getattribute__(cls, "__mro__")
            if name in type.__getattribute__(typ, "__dict__")
        ), None)
    return slots[key]


def trace(receiver, name, reader_module):
    value = getattr(receiver, name)
    typ = type(receiver)
    if issubclass(typ, ModuleType):
        key = receiver.__name__, "", name
    else:
        cls = receiver if issubclass(typ, type) else typ
        key = owner(cls, name)
    if key is None or not (key[0] == "sqlglot" or key[0].startswith("sqlglot.")):
        return value
    if PENDING:
        import_reads[key].add(reader_module)
    elif active is not None and key not in seen:
        observed[active].add(key)
        seen.add(key)
    return value


class Instrument(ast.NodeTransformer):
    def __init__(self, module, names):
        self.module = module
        self.names = names

    def visit_Attribute(self, node):
        self.generic_visit(node)
        if not isinstance(node.ctx, ast.Load) or node.attr not in self.names:
            return node
        return ast.copy_location(ast.Call(
            ast.Name("_br_read_attribute", ast.Load()),
            [node.value, ast.Constant(node.attr), ast.Constant(self.module)],
            [],
        ), node)


class Loader(importlib.machinery.SourceFileLoader):
    names = set()

    def exec_module(self, module):
        global PENDING
        PENDING += 1
        try:
            return super().exec_module(module)
        finally:
            PENDING -= 1

    def get_code(self, fullname):
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        Instrument(fullname, self.names).visit(tree)
        ast.fix_missing_locations(tree)
        return compile(tree, self.path, "exec", dont_inherit=True)


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname not in ("sqlglot", "tests") and not fullname.startswith(("sqlglot.", "tests.")):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = Loader(fullname, spec.origin)
        return spec


def install(names):
    Loader.names = set(names)
    builtins._br_read_attribute = trace
    sys.meta_path.insert(0, Finder())
