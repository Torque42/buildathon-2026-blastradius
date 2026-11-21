"""Build the per-test class attribute map from a clean pinned SQLGlot checkout."""
import argparse
import ast
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def tracked_names(source):
    names = set()
    for path in (source / "sqlglot").rglob("*.py"):
        for scope in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
            if not isinstance(scope, (ast.Module, ast.ClassDef)):
                continue
            for member in scope.body:
                if isinstance(member, ast.Assign):
                    targets = member.targets
                elif isinstance(member, ast.AnnAssign):
                    targets = [member.target]
                else:
                    continue
                names.update(t.id for t in targets if isinstance(t, ast.Name)
                             and not t.id.startswith("__"))
    return sorted(names)


def collect(source, temporary):
    names = temporary / "names.json"
    names.write_text(json.dumps(tracked_names(source)))
    trace = temporary / "attribute-trace.json"
    plugin = Path(__file__).resolve().parent / "attribute_trace"
    env = dict(os.environ, BR_ATTRIBUTE_NAMES=str(names), BR_ATTRIBUTE_TRACE=str(trace),
               PYTHONPATH=os.pathsep.join(map(str, (plugin, source))),
               PYTHONDONTWRITEBYTECODE="1", SKIP_INTEGRATION="0",
               PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1")
    subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--tb=no",
         "-p", "no:cacheprovider", "-p", "attribute_plugin"],
        check=True, cwd=source, env=env,
    )
    return trace


def pack(trace, output):
    profile = json.loads(trace.read_bytes())
    tests = sorted(profile["observed"])
    if not tests:
        raise ValueError("No attribute reads recorded")
    positions = {test: index for index, test in enumerate(tests)}
    slots = defaultdict(list)
    for test, reads in profile["observed"].items():
        for module, owner, member in reads:
            if owner:
                slots[f"{module}:{owner}:{member}"].append(positions[test])
    payload = json.dumps({"tests": tests, "slots": slots}, separators=(",", ":")).encode()
    with gzip.GzipFile(str(output), "wb", mtime=0) as stream:
        stream.write(payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Clean SQLGlot checkout at the pinned revision")
    parser.add_argument("--trace", type=Path, help="Use a previously collected full-suite trace")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1]
                        / "maps" / "observed-attributes.json.gz")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="blast-radius-attributes-") as directory:
        trace = args.trace or collect(args.source.resolve(), Path(directory))
        pack(trace, args.output)
