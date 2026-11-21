"""Collect and package pinned-source line coverage and qualified attribute reads.

Run with the locked pytest environment and a clean source checkout. The tests
catalog supplies the enabled tests and excludes skipped or unnumbered tests.
"""

import argparse
import ast
import gzip
import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile

ENTRY = Path(__file__).resolve().parents[1]


def declaration_names(source):
    names = set()
    for file in (source / "sqlglot").rglob("*.py"):
        for owner in ast.walk(ast.parse(file.read_bytes())):
            if not isinstance(owner, (ast.ClassDef, ast.Module)):
                continue
            for statement in owner.body:
                if isinstance(statement, ast.Assign):
                    targets = statement.targets
                elif isinstance(statement, ast.AnnAssign):
                    targets = [statement.target]
                else:
                    continue
                names.update(target.id for target in targets if isinstance(target, ast.Name))
    return sorted(names)


def collect(source, tests, workspace, jobs):
    names = workspace / "attributes.json"
    names.write_text(json.dumps(declaration_names(source)))
    line_traces = workspace / "lines"
    attribute_traces = workspace / "attributes"
    line_traces.mkdir()
    attribute_traces.mkdir()

    def trace(item):
        number, node_id = item
        env = dict(
            os.environ,
            PYTHONPATH=os.pathsep.join((str(Path(__file__).parent), str(source))),
            TRACE_ROOT=str(source),
            TRACE_TEST=str(number),
            TRACE_ATTR_NAMES=str(names),
            TRACE_ATTR_OUT=str(attribute_traces),
            TRACE_LINE_OUT=str(line_traces),
            SKIP_INTEGRATION="0",
            PYTHON_CPU_COUNT="2",
            OPENBLAS_NUM_THREADS="1",
        )
        subprocess.run(
            [sys.executable, "-m", "pytest", "-q", node_id],
            cwd=source,
            env=env,
            check=True,
        )

    with ThreadPoolExecutor(max_workers=jobs) as executor:
        list(executor.map(trace, tests.items()))
    return line_traces, attribute_traces


def encode_map(tests, line_traces, attribute_traces):
    lines = defaultdict(dict)
    attributes = {}
    recorded = []
    for number, node_id in tests.items():
        line_files = sorted(line_traces.glob(f"{number}-*.json"))
        attribute_files = sorted(attribute_traces.glob(f"{number}-*.json"))
        if not line_files or not attribute_files:
            continue
        line_data = [json.loads(file.read_bytes()) for file in line_files]
        if not any(line_data):
            continue
        mask = 1 << len(recorded)
        recorded.append(node_id)
        for process in line_data:
            for path, covered in process.items():
                for line in covered:
                    key = str(line)
                    lines[path][key] = lines[path].get(key, 0) | mask
        for file in attribute_files:
            for module, cls, name in json.loads(file.read_bytes()):
                if module == "sqlglot" or module.startswith("sqlglot."):
                    key = f"{module}:{cls}:{name}"
                    attributes[key] = attributes.get(key, 0) | mask
    return {"tests": recorded, "lines": lines, "attributes": attributes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("data", type=Path)
    parser.add_argument("--jobs", type=int, default=2)
    parser.add_argument("--line-traces", type=Path)
    parser.add_argument("--attribute-traces", type=Path)
    args = parser.parse_args()
    if bool(args.line_traces) != bool(args.attribute_traces):
        parser.error("supply both trace directories or neither")

    sys.argv[:] = [str(ENTRY / "selector.py"), str(args.data.resolve())]
    selector = runpy.run_path(sys.argv[0])
    test_catalog = {entry["number"]: entry["id"] for entry in selector["TJ"]}
    tests = {number: test_catalog[number] for number in sorted(selector["pool_tests"]())}

    with tempfile.TemporaryDirectory(prefix="blast-radius-pool-") as directory:
        paths = (
            (args.line_traces, args.attribute_traces)
            if args.line_traces
            else collect(args.source.resolve(), tests, Path(directory), args.jobs)
        )
        result = encode_map(tests, *paths)
        if not result["tests"]:
            raise RuntimeError("No usable process-pool traces collected")

    payload = json.dumps(result, separators=(",", ":"), sort_keys=True).encode()
    output = ENTRY / "maps" / "pool.json.gz"
    output.write_bytes(gzip.compress(payload, mtime=0))
    print(f"Wrote {output} for {len(result['tests'])} pool tests")


if __name__ == "__main__":
    main()
