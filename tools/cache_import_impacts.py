"""Index function lines executed during module initialization on the pristine pin."""
import ast
import importlib
import importlib.abc
import importlib.machinery
import marshal
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT/"cache/pinned"
sys.path.insert(0, str(PIN))
initializing = []
reads = {}
tool = 3


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        initializing.append(module.__name__)
        try:
            super().exec_module(module)
        finally:
            initializing.pop()


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname,spec.origin)
                return spec


prefix = str(PIN) + "/"
def enter(code, offset):
    if code.co_flags & 1 and code.co_filename.startswith(prefix+"sqlglot/"):
        sys.monitoring.set_local_events(tool,code,sys.monitoring.events.LINE)


def line(code, number):
    if initializing:
        filename = code.co_filename[len(prefix):]
        reads.setdefault(filename,{}).setdefault(number,set()).add(initializing[-1])


sys.meta_path.insert(0,Finder())
sys.monitoring.use_tool_id(tool,"cold initializers")
sys.monitoring.register_callback(tool,sys.monitoring.events.PY_START,enter)
sys.monitoring.register_callback(tool,sys.monitoring.events.LINE,line)
sys.monitoring.set_events(tool,sys.monitoring.events.PY_START)
for path in sorted((PIN/"sqlglot").rglob("*.py")):
    module = ".".join(path.relative_to(PIN).with_suffix("").parts).removesuffix(".__init__")
    if module.endswith(".__main__"):
        continue
    try:
        importlib.import_module(module)
    except ImportError:
        # Optional integrations absent from the image cannot supply a pristine replay.
        continue
sys.monitoring.set_events(tool,0)

# Record class schemas that copy a module-level required/optional field map.
tables, users = {}, {}
for path in sorted((PIN/"sqlglot/expressions").glob("*.py")):
    relative = str(path.relative_to(PIN))
    module = relative[:-3].replace("/",".")
    tree = ast.parse(path.read_bytes())
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets)==1 and isinstance(stmt.targets[0],ast.Name) and isinstance(stmt.value,ast.Dict):
            if stmt.value.values and all(isinstance(v,ast.Constant) and type(v.value) is bool for v in stmt.value.values):
                tables.setdefault(relative,[]).append((stmt.value.lineno,stmt.value.end_lineno,stmt.targets[0].id))
        if not isinstance(stmt,ast.ClassDef):
            continue
        for member in stmt.body:
            targets=member.targets if isinstance(member,ast.Assign) else [member.target] if isinstance(member,ast.AnnAssign) else []
            if not any(isinstance(t,ast.Name) and t.id=="arg_types" for t in targets):
                continue
            expr = member.value
            values = [v for k,v in zip(expr.keys,expr.values) if k is None] if isinstance(expr, ast.Dict) else [expr]
            for value in values:
                if isinstance(value,ast.Name):
                    users.setdefault(value.id,[]).append(module+":"+stmt.name)
data = ({p:{n:tuple(sorted(mods)) for n,mods in lines.items()} for p,lines in reads.items()},tables,users)
(ROOT/"cache/import-impacts").write_bytes(marshal.dumps(data))
