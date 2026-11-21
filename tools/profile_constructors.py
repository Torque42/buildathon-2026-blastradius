"""Collect isolated, pristine constructor observations including spawned workers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

TESTS = (
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_generator.py::TestGenerator::test_generator_memory",
)


def main():
    source = Path(sys.argv[1]).resolve()
    trace = Path(__file__).resolve().parent / "constructor_trace"
    records = {}
    for test in TESTS:
        with tempfile.TemporaryDirectory() as log:
            env = dict(os.environ, BR_CONSTRUCTOR_LOG=log, PYTHON_CPU_COUNT="2",
                       OPENBLAS_NUM_THREADS="1", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
                       PYTHONPATH=os.pathsep.join(map(str, (trace, source))))
            run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test],
                                 cwd=source, env=env, text=True, stdout=subprocess.PIPE, check=True)
            if "1 passed" not in run.stdout:
                raise RuntimeError(run.stdout)
            rows = [json.loads(f.read_bytes()) for f in Path(log).glob("*.json")]
            active = [r for r in rows if r["calls"].get("Parser")]
            if not rows or not any("Parser" in r["installed"] for r in rows) or any("Parser" not in r["installed"] for r in active):
                raise RuntimeError(f"Incomplete parser trace for {test}")
            if "::test_execute_" in test and sum("SetOperation" in r["installed"] for r in rows) < 2:
                raise RuntimeError(f"Missing spawned executor trace for {test}")
            def union(key):
                return sorted(set().union(*(r[key] for r in rows)))
            records[test] = dict(installed=union("installed"), calls={owner:sum(r["calls"].get(owner,0) for r in rows) for owner in ("Parser","SetOperation")},
                                 read=union("read"), omitted=union("omitted"), uncertain=union("uncertain"))
            print(test, records[test]["calls"], file=sys.stderr, flush=True)
    print(json.dumps(records, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
