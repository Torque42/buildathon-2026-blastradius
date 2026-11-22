"""Trace actual owners of attributes read by the pristine node-limit regression test."""

import ast
import builtins
from collections import defaultdict
import gzip
import importlib.abc
import importlib.machinery
import marshal
import os
from pathlib import Path
import sys
import types
import unittest


def main():
    root, out = (Path(v).resolve() for v in sys.argv[1:3])
    sys.path.insert(0, str(root))
    os.chdir(root)
    stage = None
    seen = defaultdict(set)
    classes = {}
    extra = defaultdict(set)

    def observe(obj, name):
        if stage is None or type(name) is not str:
            return obj
        if type(obj) is types.ModuleType:
            module = obj.__name__
            if module.startswith("sqlglot"):
                seen[stage].add((module, "", name))
            return obj
        if type(obj) is super:
            extra[stage].add(name)
            return obj
        cls = obj if isinstance(obj, type) else type(obj)
        slot = id(cls)
        if slot not in classes:
            mod = type.__getattribute__(cls, "__module__")
            scope = (mod, type.__getattribute__(cls, "__qualname__")) if mod.startswith("sqlglot") else None
            classes[slot] = cls, scope
        scope = classes[slot][1]
        if scope is not None:
            seen[stage].add((*scope, name))
        return obj

    def lookup(receiver, name, *fallback):
        observe(receiver, name)
        return getattr(receiver, name, *fallback)

    def has(receiver, name):
        observe(receiver, name)
        return hasattr(receiver, name)

    class Instrument(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load):
                node.value = ast.copy_location(ast.Call(ast.Name("_br_observe_owner", ast.Load()),
                                                       [node.value, ast.Constant(node.attr)], []), node.value)
            return node

        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
                node.func.id = "_br_lookup" if node.func.id == "getattr" else "_br_has"
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(Path(self.path).read_bytes(), self.path)
            tree = ast.fix_missing_locations(Instrument().visit(tree))
            return compile(tree, self.path, "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if not (fullname == "sqlglot" or fullname.startswith("sqlglot.") or fullname == "tests.test_parser"):
                return
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
                return spec

    builtins._br_observe_owner, builtins._br_lookup, builtins._br_has = observe, lookup, has
    sys.meta_path.insert(0, Finder())
    from tests import test_parser
    original = test_parser.parse_one
    calls = []

    def wrapped(sql, *args, **kwargs):
        nonlocal stage
        index = len(calls)
        calls.append(len(sql))
        stage = str(index)
        try:
            return original(sql, *args, **kwargs)
        finally:
            stage = "outside"

    stage = "outside"
    test_parser.parse_one = wrapped
    result = unittest.TestResult()
    unittest.TestSuite([test_parser.TestParser("test_max_nodes")]).run(result)
    stage = None
    if not result.wasSuccessful() or result.testsRun != 1 or result.skipped or len(calls) != 6:
        raise ValueError((result.failures, result.errors, calls))
    data = (tuple(sys.version_info[:2]), calls,
            {k:sorted(v) for k,v in seen.items()}, {k:sorted(v) for k,v in extra.items()})
    out.write_bytes(gzip.compress(marshal.dumps(data), mtime=0))
    print("pristine node-limit test", calls, {k:len(v) for k,v in seen.items()}, file=sys.stderr)


if __name__ == "__main__":
    main()
