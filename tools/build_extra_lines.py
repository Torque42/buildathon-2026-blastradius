import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

base = Path(__file__).resolve().parent
source, python, manifest = map(Path, sys.argv[1:4])
import gzip
known = set(json.load(gzip.open(base.parent / "maps/maps.json.gz"))["tests"])
profiles = {}
details = {}
for test in json.loads(manifest.read_text())["tests"]:
    node = test["id"]
    if node in known or any(item.partition("[")[0] == node for item in known):
        continue
    with tempfile.TemporaryDirectory() as result:
        env = dict(os.environ, PIN_ROOT=str(source), PIN_OUT=result,
                   PATH=str(python.parent) + ":" + os.environ["PATH"],
                   PYTHONPATH=os.pathsep.join(map(str, (base / "isolated_trace", base / "key_trace", source))),
                   SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2")
        run = subprocess.run([python, "-m", "pytest", "-p", "no:cacheprovider", "-q", node],
                             cwd=source, env=env, capture_output=True, text=True)
        if run.returncode or "1 passed" not in run.stdout:
            print(node, run.stdout[-500:], flush=True)
            continue
        reads, files, calls, keys, inspected = set(), {}, {}, {}, {}
        logs = list(Path(result).glob("*.json"))
        if "subprocess" in (source / test["file"]).read_text() and len(logs) < 2:
            continue
        for log in logs:
            raw, lines, entered, values, code = json.loads(log.read_text())
            for path, names in code.items():
                inspected.setdefault(path,set()).update(names)
            for field, items in values.items():
                keys.setdefault(field,set()).update(items)
            reads.update(raw)
            for path, names in entered.items():
                calls.setdefault(path, set()).update(names)
            for path, hits in lines.items():
                files.setdefault(path, set()).update(hits)
        profiles[node] = [sorted(reads), {path: sorted(hits) for path,hits in files.items()}]
        details[node] = [sorted(reads), {path: sorted(names) for path,names in calls.items()}, {field: sorted(items) for field,items in keys.items()}, {path: sorted(names) for path,names in inspected.items()}]
        print(node, len(files), len(logs), flush=True)
(base.parent / "maps/extra-calls.json.gz").write_bytes(gzip.compress(json.dumps(details).encode(), mtime=0))
