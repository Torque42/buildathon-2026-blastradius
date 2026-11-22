"""Index self-contained, deterministic assertion methods from the pristine suite."""
import ast
import marshal
import os
import sys
from pathlib import Path
import tarfile

ROOT = Path(__file__).resolve().parents[1]
PIN = ROOT/"cache/pinned"
with tarfile.open(ROOT/"maps/text-fixtures.tar.xz") as archive:
    archive.extractall(PIN, filter="data")

allowed_files = {"test_anonymize","test_build","test_diff","test_expressions","test_generator","test_helper",
                 "test_jsonpath","test_parser","test_schema","test_serde","test_time","test_tokens","test_transforms",
                 "test_transpile","test_optimize_cross_joins","test_simplify_between"}
forbidden = {"mock","patch","sys","os","subprocess","tempfile","importlib","time","random","pytest","warnings",
             "inspect","tracemalloc","gc","socket","multiprocessing","logging","threading","setattr","delattr",
             "open","Path","getenv","skipTest","setUpClass","tearDownClass","register","assertLogs"}
permitted = set()
for file in (PIN/"tests").rglob("test_*.py"):
    relative = file.relative_to(PIN)
    if "dialects" not in relative.parts and file.stem not in allowed_files:
        continue
    tree = ast.parse(file.read_bytes())
    if any(isinstance(n, ast.FunctionDef) and n.name in ("setUpModule","tearDownModule") for n in tree.body):
        continue
    for cls in tree.body:
        if not isinstance(cls,ast.ClassDef) or cls.decorator_list:
            continue
        if any(isinstance(n,ast.FunctionDef) and n.name in ("setUpClass","tearDownClass") for n in cls.body):
            continue
        for fn in cls.body:
            if not isinstance(fn,ast.FunctionDef) or not fn.name.startswith("test_") or fn.decorator_list:
                continue
            nodes = list(ast.walk(fn))
            if any(isinstance(n,(ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal, ast.ClassDef)) for n in nodes):
                continue
            names = {getattr(n,"id",getattr(n,"attr",None)) for n in nodes}
            if names & forbidden or not any(isinstance(n,ast.Assert) or isinstance(n,ast.Attribute)
                                            and n.attr.startswith(("assert","validate")) for n in nodes):
                continue
            if any(isinstance(n,ast.Attribute) and isinstance(n.ctx,(ast.Store,ast.Del)) for n in nodes):
                continue
            permitted.add(f"{relative}::{cls.name}::{fn.name}")
# A proof needs a passing pristine test with observable assertions. The
# deployed selector compares the ordered assertion locations and multiplicities.
sys.path[:0] = [str(ROOT), str(PIN)]
os.chdir(PIN)
os.environ["SKIP_INTEGRATION"] = "0"
from shortlist import check_test, witness
state = witness()
fingerprints = {}
for node in sorted(permitted):
    try:
        digest = check_test(node, state)
        if digest is not None:
            fingerprints[node] = digest
    except Exception:
        pass
if not fingerprints:
    raise RuntimeError("No reproducible pristine assertions")
(ROOT/"cache/replayable-tests").write_bytes(marshal.dumps(fingerprints))
print(len(fingerprints), "pristine assertion witnesses")
