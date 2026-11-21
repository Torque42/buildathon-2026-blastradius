"""Observe pure factory inputs and the process context of module guards on the pin."""
import ast
import atexit
import builtins
import importlib.abc
import importlib.machinery
import json
import os
import sys
from pathlib import Path

config = json.loads(Path(os.environ["BR_PROBE_PLAN"]).read_bytes())
readers = {}
contexts = {}
active = "*"
file_scope = os.environ["BR_PROBE_SCOPE"]

def read(key, value):
    if type(value) in (str, int, bool):
        readers.setdefault(key + ":" + type(value).__name__ + ":" + repr(value), set()).add(active)
    return value

def observe_guard(site):
    # For the proof we use only the guaranteed presence of pytest in a pytest process.
    contexts.setdefault(site, set()).add(tuple(sorted(sys.modules)))
    return False

def invoke(name, *args):
    return ast.Call(ast.Name(name, ast.Load()), list(args), [])

class Loader(importlib.machinery.SourceFileLoader):
    def get_code(self, fullname):
        path = "sqlglot/" + self.path.split("/sqlglot/", 1)[1]
        tree = ast.parse(self.get_data(self.path), filename=self.path)
        definitions = config["definitions"].get(path, {})
        guards = {(n, a) for n, a, b in config["guards"].get(path, [])}
        for st in tree.body:
            if isinstance(st, (ast.Assign, ast.AnnAssign)) and st.value and (st.value.lineno, st.value.col_offset) in guards:
                site = f"{path}:{st.value.lineno}:{st.value.col_offset}"
                value = st.value
                st.value = ast.copy_location(ast.BoolOp(ast.Or(), [invoke("_br_guard_state", ast.Constant(site)), value]), value)
            if not isinstance(st, ast.FunctionDef) or st.name not in definitions:
                continue
            capture = set(definitions[st.name]); prefix = fullname + "." + st.name + ":"
            class TagReads(ast.NodeTransformer):
                def visit_Name(self, node):
                    if node.id in capture and isinstance(node.ctx, ast.Load):
                        return ast.copy_location(invoke("_br_captured_read", ast.Constant(prefix + node.id), node), node)
                    return node
            # The site scan has excluded dynamic-local access and shadowing.
            st.body = [TagReads().visit(stmt) for stmt in st.body]
        return compile(ast.fix_missing_locations(tree), self.path, "exec", dont_inherit=True)

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        rel = fullname.replace(".", "/") + ".py"
        if rel not in config["definitions"] and rel not in config["guards"]:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            spec.loader = Loader(fullname, spec.origin)
        return spec

def save():
    out = Path(os.environ["BR_PROBE_OUT"]) / (str(os.getpid()) + ".json")
    out.write_text(json.dumps({"reads": {k: sorted(v) for k, v in readers.items()},
                               "guards": {k: sorted(v) for k, v in contexts.items()}}))

builtins._br_captured_read = read
builtins._br_guard_state = observe_guard
sys.meta_path.insert(0, Finder())
atexit.register(save)
