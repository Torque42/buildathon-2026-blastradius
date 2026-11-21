import ast
import atexit
import builtins
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys
from types import ModuleType

if os.environ.get("TRACE_TEST"):
    root = Path(os.environ["TRACE_ROOT"])
    names = frozenset(json.loads(Path(os.environ["TRACE_ATTR_NAMES"]).read_text()))
    used = set()
    seen = set()
    sqlglot_source = str(root / 'sqlglot') + os.sep

    def import_time(frame):
        while frame is not None:
            code = frame.f_code
            if code.co_name == '<module>' and code.co_filename.startswith(sqlglot_source):
                return True
            frame = frame.f_back
        return False

    def trace_attr(receiver, name):
        value = getattr(receiver, name)
        receiver_type = type(receiver)
        if issubclass(receiver_type, ModuleType):
            used.add((receiver.__name__, "", name))
            return value
        cls = receiver if issubclass(receiver_type, type) else receiver_type
        key = cls, name
        if key not in seen and not import_time(sys._getframe(1)):
            seen.add(key)
            for owner in type.__getattribute__(cls, "__mro__"):
                namespace = type.__getattribute__(owner, "__dict__")
                if name in namespace:
                    used.add((type.__getattribute__(owner, "__module__"), type.__getattribute__(owner, "__qualname__"), name))
                    break
        return value

    builtins._blast_radius_trace_attr = trace_attr

    class Transform(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load) and node.attr in names and not node.attr.startswith("__"):
                call = ast.Call(
                    func=ast.Name(id="_blast_radius_trace_attr", ctx=ast.Load()),
                    args=[node.value, ast.Constant(value=node.attr)],
                    keywords=[],
                )
                return ast.copy_location(call, node)
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            source = self.get_data(self.path)
            tree = ast.parse(source, filename=self.path)
            Transform().visit(tree)
            ast.fix_missing_locations(tree)
            return compile(tree, self.path, "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "sqlglot" and not fullname.startswith("sqlglot."):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
            return spec

    sys.meta_path.insert(0, Finder())

    def save():
        dest = Path(os.environ["TRACE_ATTR_OUT"]) / f"{os.environ['TRACE_TEST']}-{os.getpid()}.json"
        dest.write_text(json.dumps(sorted(used)))

    atexit.register(save)

    if os.environ.get("TRACE_LINE_OUT"):
        lines = {}
        root_prefix = str(root) + os.sep

        def trace_line(code, line):
            path = code.co_filename
            if path.startswith(sqlglot_source) and code.co_name != "<module>":
                lines.setdefault(path[len(root_prefix):], set()).add(line)
            return sys.monitoring.DISABLE

        def save_lines():
            dest = Path(os.environ["TRACE_LINE_OUT"]) / f"{os.environ['TRACE_TEST']}-{os.getpid()}.json"
            dest.write_text(json.dumps({path: sorted(covered) for path, covered in lines.items()}))

        atexit.register(save_lines)
        sys.monitoring.use_tool_id(4, "blast-radius-pool")
        sys.monitoring.register_callback(4, sys.monitoring.events.LINE, trace_line)
        sys.monitoring.set_events(4, sys.monitoring.events.LINE)
