"""Profile independent pristine pytest files, including their collection and child processes."""
import gzip
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from input_sites import scan

root, catalog, dest = [Path(v).resolve() for v in sys.argv[1:4]]
tools = Path(__file__).resolve().parent
tests = json.loads(catalog.read_bytes())["tests"]
pools = set(json.load(gzip.open(tools.parent / "maps/pool.json.gz"))["tests"])
plan = scan(root)
nodes = {}
for test in tests:
    nodes.setdefault(test["file"], set()).add(test["id"])
with tempfile.TemporaryDirectory() as temp:
    planfile = Path(temp) / "plan.json"
    planfile.write_text(json.dumps(plan))
    env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
               PYTHONPATH=os.pathsep.join(map(str, [tools/"input_trace", root])),
               BR_PROBE_PLAN=str(planfile), SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2",
               OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    def profile(file):
        with tempfile.TemporaryDirectory() as tmp:
            settings = dict(env, BR_PROBE_OUT=tmp, BR_PROBE_SCOPE=file)
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                   "-p", "input_plugin", file], env=settings, cwd=root, capture_output=True, timeout=300)
            logs = list(Path(tmp).glob("*.json"))
            done = Path(tmp)/"catalog"
            if proc.returncode or not done.is_file() or not nodes[file].issubset(json.loads(done.read_bytes())) or not logs:
                print("unavailable", file, proc.returncode, proc.stdout[-200:], flush=True)
                return file, None
            if pools & nodes[file] and len(logs) < 2:
                return file, None
            reads = {}; guards = {}
            for log in logs:
                data = json.loads(log.read_bytes())
                for key, names in data["reads"].items():
                    reads.setdefault(key, set()).update(nodes[file] if "*" in names else names)
                for key, values in data["guards"].items():
                    guards.setdefault(key, set()).update(tuple(value) for value in values)
            return file, {"reads": {k: sorted(v) for k,v in reads.items()}, "guards": {k: sorted(v) for k,v in guards.items()}}
    with ThreadPoolExecutor(max_workers=4) as threads:
        profiles = dict(threads.map(profile, sorted(nodes)))
payload = {"catalog": {f: sorted(ns) for f, ns in nodes.items() if profiles[f] is not None},
           "sites": plan["sites"], "guard_sites": plan["guards"], "reads": {}, "guards": {}}
for file, profile in profiles.items():
    if profile:
        for key, ids in profile["reads"].items():
            payload["reads"].setdefault(key, []).extend(ids)
        for key, values in profile["guards"].items():
            payload["guards"].setdefault(key, {})[file] = values
dest.write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
print(len(payload["catalog"]), "files", len(payload["reads"]), "read values", len(payload["guard_sites"]), "guard files", flush=True)
