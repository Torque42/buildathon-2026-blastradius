"""Collect value shapes, validation inputs and parser dispatch on pristine SQLGlot."""
import ast
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
from types import FunctionType
import sys


def install():
    root = Path(os.environ["BR_OPERAND_ROOT"]).resolve()
    source_prefix = str(root / "sqlglot") + os.sep
    observations = {}
    source_reads = set()
    missing, opaque, factories, arguments = {}, set(), set(), {}
    code_reads = {}
    tracing_audit = False

    def audit(event, args):
        nonlocal tracing_audit
        if tracing_audit:
            return
        if event == "object.__getattr__" and args[1] == "__code__" and type(args[0]) is FunctionType:
            tracing_audit = True
            try:
                code = args[0].__code__
                if code.co_filename.startswith(source_prefix):
                    rel = os.path.relpath(code.co_filename, root)
                    code_reads.setdefault(rel, set()).update(n for _, _, n in code.co_lines() if n is not None)
            finally:
                tracing_audit = False
        elif event == "open" and isinstance(args[0], str) and args[0].endswith(".py"):
            filename = os.path.abspath(args[0])
            if not filename.startswith(source_prefix):
                return
            # The instrumenting loader reads .py even when the regular
            # importer could use a .pyc. Ignore that read, retain application
            # opens and source inspection via linecache or inspect.
            frame = sys._getframe(1)
            while frame and frame.f_code.co_filename.startswith("<frozen importlib"):
                frame = frame.f_back
            if frame and frame.f_code is not loader_code:
                source_reads.add(os.path.relpath(filename, root))



    def shape(left, right, site):
        safe = (type(left) is tuple and type(right) is tuple and len(left) != len(right)
                and all(type(item) is int for item in left) and all(type(item) is int for item in right))
        observations.setdefault(site, {})[safe] = ("unequal", "length") if safe else ("unknown", "unknown")
        return left

    builtins._br_tuple_shape = shape

    def owner(cls):
        return cls.__module__ + ":" + cls.__qualname__

    def check_args(instance):
        cls = type(instance)
        if type(instance.args) is not dict or type(cls.arg_types) is not dict:
            opaque.add(owner(cls))
            return
        absent = {key for key in cls.arg_types if instance.args.get(key) is None or
                  type(instance.args.get(key)) is list and not instance.args[key]}
        if absent.intersection(cls.required_args):
            opaque.add(owner(cls))
        missing.setdefault(owner(cls), set()).update(absent)

    def read(receiver, field):
        value = getattr(receiver, field)
        cls = receiver if isinstance(receiver, type) else type(receiver)
        opaque.add(owner(cls))
        return value

    def dispatch(site, args, kwargs):
        if all(type(value) in (str, bool, int, type(None)) for value in (*args, *kwargs.values())):
            factories.add(json.dumps([site, list(args), kwargs], sort_keys=True))
        else:
            factories.add(json.dumps([site, None, None]))

    builtins._br_check_args = check_args
    builtins._br_opaque_read = read
    builtins._br_dispatch = dispatch

    def primitive(site, value):
        arguments[site] = arguments.get(site, True) and type(value) is str
        return value
    builtins._br_primitive = primitive

    class Transformer(ast.NodeTransformer):
        def __init__(self, path):
            self.path = path

        def instrument(self, tree):
            parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
            for node in list(ast.walk(tree)):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "alias_" and len(node.args) == 1:
                    child = node.args[0]
                    if isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "table_" and len(child.args) == 1 and not child.keywords:
                        site = self.path + ":" + str(node.lineno) + ":" + str(node.col_offset)
                        for kw in node.keywords:
                            if kw.arg == "alias":
                                kw.value = ast.copy_location(ast.Call(ast.Name("_br_primitive", ast.Load()), [ast.Constant(site), kw.value], []), kw.value)
                        child.args[0] = ast.copy_location(ast.Call(ast.Name("_br_primitive", ast.Load()), [ast.Constant(site), child.args[0]], []), child.args[0])
                if isinstance(node, ast.FunctionDef) and self.path == "sqlglot/expressions/core.py" and node.name == "error_messages" and isinstance(parents[node], ast.ClassDef) and parents[node].name == "Expression":
                    node.body.insert(0, ast.parse("_br_check_args(self)").body[0])
                if isinstance(node, ast.FunctionDef) and node.args.vararg and node.args.kwarg and len(node.body) == 2:
                    inner, ret = node.body
                    if isinstance(inner, ast.FunctionDef) and isinstance(ret, ast.Return) and isinstance(ret.value, ast.Name) and ret.value.id == inner.name:
                        site = self.path + ":" + node.name
                        event = ast.Expr(ast.Call(ast.Name("_br_dispatch", ast.Load()), [ast.Constant(site), ast.Name(node.args.vararg.arg, ast.Load()), ast.Name(node.args.kwarg.arg, ast.Load())], []))
                        inner.body.insert(0, event)
                if not isinstance(node, ast.Attribute) or node.attr not in {"arg_types","required_args"} or not isinstance(node.ctx, ast.Load):
                    continue
                parent = parents[node]
                here = parent
                while here and not isinstance(here, ast.FunctionDef):
                    here = parents.get(here)
                if self.path == "sqlglot/expressions/core.py" and here and here.name == "__init_subclass__" and node.attr == "arg_types":
                    continue
                if self.path == "sqlglot/expressions/core.py" and here and here.name == "error_messages" and node.attr == "required_args":
                    continue
                if node.attr == "arg_types":
                    if isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is node:
                        continue
                    if isinstance(parent, ast.Compare) and len(parent.ops) == 1 and isinstance(parent.ops[0], (ast.In, ast.NotIn)) and parent.comparators[0] is node:
                        continue
                    if isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id in {"len","tuple","set","zip"}:
                        continue
                call = ast.copy_location(ast.Call(ast.Name("_br_opaque_read", ast.Load()), [node.value, ast.Constant(node.attr)], []), node)
                for field, value in ast.iter_fields(parent):
                    if value is node:
                        setattr(parent, field, call)
                    elif isinstance(value, list):
                        for index, item in enumerate(value):
                            if item is node:
                                value[index] = call
            return self.visit(tree)

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE)) and isinstance(node.comparators[0], ast.Tuple) and all(isinstance(element, ast.Constant) and type(element.value) is int for element in node.comparators[0].elts):
                site = f"{self.path}:{node.lineno}:{node.col_offset}"
                node.left = ast.copy_location(ast.Call(ast.Name("_br_tuple_shape", ast.Load()), [node.left, node.comparators[0], ast.Constant(site)], []), node.left)
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(self.get_data(self.path), self.path)
            tree = Transformer(os.path.relpath(self.path, root)).instrument(tree)
            return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.startswith(source_prefix) and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    # Keep a reference before installing the introspection watcher.
    loader_code = Loader.get_code.__code__
    sys.addaudithook(audit)
    sys.meta_path.insert(0, Finder())

    def save():
        data = {site: sorted(set(pairs.values())) for site, pairs in observations.items()}
        with gzip.open(Path(os.environ["BR_OPERAND_OUT"]) / f"{os.getpid()}.json.gz", "wt") as stream:
            json.dump({"arguments": arguments, "missing": {cls: sorted(keys) for cls,keys in missing.items()}, "opaque": sorted(opaque), "factories": sorted(factories), "sites": data, "source_reads": sorted(source_reads),
                       "code_reads": {p: sorted(lines) for p, lines in code_reads.items()}}, stream)
    atexit.register(save)


if os.environ.get("BR_OPERAND_ROOT"):
    install()
