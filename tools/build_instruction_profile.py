"""Collect cold coverage by test file and by expensive individual node."""
import gzip
import hashlib
import json
import marshal
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

root, catalog, scratch, dest = [Path(v).resolve() for v in sys.argv[1:5]]
count = int(sys.argv[5])
tools = Path(__file__).resolve().parent
tests = json.loads(catalog.read_bytes())["tests"]
pools = set(json.load(gzip.open(tools.parent/"maps/pool.json.gz"))["tests"])
files = {t["file"] for t in tests}
nodes = sorted(files) + [t["id"] for t in sorted(tests, key=lambda t: (-t["seconds"], t["id"]))[:count]]
scratch.mkdir(exist_ok=True, parents=True)
env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
           PYTHONPATH=os.pathsep.join(map(str, (tools/"instruction_trace", root))), BR_PIN=str(root),
           SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")

def collect(node):
    out = scratch/(hashlib.sha256(node.encode()).hexdigest()[:24] + ".gz")
    if out.exists():
        return node, out
    with tempfile.TemporaryDirectory() as tmp:
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", node],
                                cwd=root, env=dict(env, BR_OFFSETS_OUT=tmp), capture_output=True, timeout=300)
        logs = list(Path(tmp).glob("*.rec"))
        has_pool = node in pools or node in files and any(p.startswith(node + "::") for p in pools)
        if result.returncode or b" passed" not in result.stdout or not logs or has_pool and len(logs) < 2:
            print("unprofiled", node, result.returncode, result.stdout[-150:], flush=True)
            return node, None
        merged = {}
        for log in logs:
            for key, raw in marshal.loads(log.read_bytes()).items():
                merged[key] = merged.get(key, 0) | int.from_bytes(raw, "little")
        if not any(key[0] == "sqlglot/__init__.py" for key in merged):
            return node, None
        out.write_bytes(gzip.compress(marshal.dumps((node, merged)), compresslevel=1, mtime=0))
        return node, out

# A file trace includes collection, class fixtures, teardown and child processes.
# A successful isolated individual trace can refine that broader fallback.
catalog_ids = []
locations = {}
with ThreadPoolExecutor(max_workers=5) as threads:
    for node, output in threads.map(collect, nodes):
        if output is None:
            continue
        name, mask_map = marshal.loads(gzip.decompress(output.read_bytes()))
        if name != node:
            raise ValueError(node)
        bit = 1 << len(catalog_ids)
        catalog_ids.append(node)
        for (path, member), mask in mask_map.items():
            groups = locations.setdefault(path, {}).setdefault(member, {})
            groups[mask] = groups.get(mask, 0) | bit
        if len(catalog_ids) % 50 == 0:
            print("completed", len(catalog_ids), flush=True)
dest.write_bytes(gzip.compress(marshal.dumps((sys.version_info[:2], catalog_ids, locations)), mtime=0))
print("saved", len(catalog_ids), dest.stat().st_size, flush=True)
