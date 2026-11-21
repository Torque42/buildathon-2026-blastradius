"""Pack per-test method receiver traces into an index keyed by class and method."""
import argparse
import ast
from collections import defaultdict
import csv
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile


def class_slots(source):
    result = {}
    for path in sorted((source / "sqlglot").rglob("*.py")):
        module = path.relative_to(source).as_posix().removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        tree = ast.parse(path.read_bytes())
        def visit(body, parents=()):
            for statement in body:
                if isinstance(statement, ast.ClassDef):
                    owner = (*parents, statement.name)
                    result[f"{module}:{'.'.join(owner)}"] = {
                        stmt.name for stmt in statement.body
                        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                    }
                    visit(statement.body, owner)
        visit(tree.body)
    return result


def pack(trace_files, tests, source):
    positions = {test["id"]: index for index, test in enumerate(tests)}
    defined = class_slots(source)
    methods = defaultdict(dict)
    startup = set()
    for filename in trace_files:
        with filename.open(newline="") as stream:
            for test, module, cls, name in csv.reader(stream):
                owner = f"{module}:{cls}"
                if owner not in defined or name.startswith("__") or name in defined[owner]:
                    continue
                if test == "@collection":
                    startup.add(f"{owner}:{name}")
                elif test in positions:
                    table = methods[owner]
                    table[name] = table.get(name, 0) | (1 << positions[test])
    return {"tests": list(positions), "receivers": methods, "early": sorted(startup)}


def collect(source, data, traces, jobs):
    entry = Path(__file__).resolve().parents[1]
    selector_path = entry / "selector.py"
    previous = sys.argv
    try:
        sys.argv = [str(selector_path), str(data)]
        selector = runpy.run_path(str(selector_path))
    finally:
        sys.argv = previous
    test_ids = {test["number"]: test["id"] for test in selector["TJ"]}
    pool = [test_ids[number] for number in sorted(selector["pool_tests"]())]
    probes = entry / "tools" / "receiver_trace"
    shared = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join(map(str, (probes, source))),
        BR_RECEIVER_TRACE=str(traces),
        BR_RECEIVER_SOURCE=str(source),
        PYTHONDONTWRITEBYTECODE="1",
        SKIP_INTEGRATION="0",
        PYTHON_CPU_COUNT="2",
        OPENBLAS_NUM_THREADS="1",
    )
    command = [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"]

    def run(test, arguments):
        env = shared.copy()
        if test is not None:
            env["BR_RECEIVER_TEST"] = test
        else:
            env.pop("BR_RECEIVER_TEST", None)
        subprocess.run(command + arguments, cwd=source, env=env, check=True)

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        suite = executor.submit(run, None, ["tests", "-p", "receiver_plugin"])
        collection = executor.submit(run, "@collection", ["tests", "--collect-only"])
        list(executor.map(lambda node_id: run(node_id, [node_id]), pool))
        suite.result()
        collection.result()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("data", type=Path, help="Directory containing tests.json")
    parser.add_argument("--traces", type=Path, help="Reuse previously collected trace CSVs")
    parser.add_argument("--jobs", type=int, default=2)
    args = parser.parse_args()
    source, data = args.source.resolve(), args.data.resolve()
    tests = json.loads((data / "tests.json").read_bytes())["tests"]
    with tempfile.TemporaryDirectory(prefix="blast-radius-receivers-") as directory:
        traces = args.traces.resolve() if args.traces else Path(directory)
        if args.traces is None:
            collect(source, data, traces, args.jobs)
        result = pack(sorted(traces.glob("*.csv")), tests, source)
    if not result["receivers"]:
        raise RuntimeError("No usable inherited dispatch was recorded")
    output = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
    target = Path(__file__).resolve().parents[1] / "maps" / "override-methods.json.gz"
    target.write_bytes(gzip.compress(output, mtime=0))
    print(f"{len(result['receivers'])} receiver classes, {len(result['early'])} collection-time calls")
