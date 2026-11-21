"""Measure cold, pristine import and read dependencies separately for each test file."""
import gzip
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

tools = Path(__file__).resolve().parent
root, catalog = (Path(x).resolve() for x in sys.argv[1:])
tests = json.loads(catalog.read_bytes())["tests"]
files = {test["file"] for test in tests}
targets = sorted(files) + sorted(test["id"] for test in tests if test["seconds"] >= 0.4)
profiles = {}
env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
           BR_PIN=str(root), PYTHONPATH=os.pathsep.join(map(str, [tools/"import_trace", root])),
           PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1")


def measure(node):
    with tempfile.TemporaryDirectory() as scratch:
        log = Path(scratch)/"reads"
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
                                env=dict(env, BR_IMPORT_LOG=str(log)), cwd=root, capture_output=True)
        if result.returncode or not log.is_file() or b" passed" not in result.stdout:
            print("no profile", node, result.returncode, flush=True)
            return node, None
        paths = sorted(set(log.read_text().splitlines()))
        if "sqlglot/__init__.py" not in paths:
            return node, None
        return node, paths


with ThreadPoolExecutor(max_workers=4) as pool:
    for node, paths in pool.map(measure, targets):
        if paths:
            profiles[node] = paths
catalog_ids = sorted(test["id"] for test in tests)
payload = {"catalog": catalog_ids, "profiles": profiles}
(tools.parent/"maps"/"isolated-imports.json.gz").write_bytes(
    gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
print("completed", len(profiles), "of", len(targets), flush=True)
