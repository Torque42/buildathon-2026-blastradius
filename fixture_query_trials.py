"""Run changed executor fixture rows through the pinned test's two SQL engines."""
import json
import os
from pathlib import Path
import pickle
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PIN = Path("/opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa")
PATHS = {f"tests/fixtures/optimizer/tpc-{letter}/tpc-{letter}.sql": kind
         for letter,kind in (("ds","tpcds"),("h","tpch"))}
NODES = {"tpcds": {"execute":"tests/test_executor.py::TestExecutor::test_execute_tpcds"},
         "tpch": {"execute":"tests/test_executor.py::TestExecutor::test_execute_tpch",
                  "optimized":"tests/test_executor.py::TestExecutor::test_optimized_tpch"}}


def rows_for(selector, patch, path):
    import fixture_inputs
    data, _ = fixture_inputs.profiles()
    old = data["contents"].get(path)
    if not old or set(data["catalog"]) != set(selector.NUM):
        return
    lines = old.splitlines()
    for h in patch["hunks"]:
        i = max(0, h["os"]-1)
        previous = [s[1:].rstrip("\r") for s in h["lines"] if s.startswith((" ", "-"))]
        if lines[i:i+len(previous)] != previous:
            return
    new = selector.apply(old, patch["hunks"])[0]
    try:
        before, after = fixture_inputs.pairs(old), fixture_inputs.pairs(new)
    except (ValueError, IndexError):
        return
    if not before or len(after) != len(before):
        return
    changed = [b for a,b in zip(before,after) if a != b]
    if 1 <= len(changed) <= 4 and sum(len(b)+len(c) for _,b,c in changed) < 64000:
        return changed


def refine(selector, text, selected):
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return selected
    patch = patches[0]
    path = patch["old"]
    if path not in PATHS or patch["new"] != path:
        return selected
    kind = PATHS[path]
    todo = {mode:selector.NUM[node] for mode,node in NODES[kind].items() if selector.NUM.get(node) in selected}
    if not todo:
        return selected
    gain = (selector.worker_cost(selector.split(selected))
            - selector.worker_cost(selector.split(selected-set(todo.values()))))/1000
    if gain < 1.5:
        return selected
    rows = rows_for(selector, patch, path)
    if not rows:
        return selected
    try:
        r = subprocess.run([sys.executable, "-I", str(ROOT/"fixture_query_trials.py")],
                           input=json.dumps((kind,list(todo),rows)).encode(), capture_output=True,
                           env=dict(os.environ, SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1"),
                           timeout=min(6.0, gain*0.45))
        if r.returncode == 0:
            proven = json.loads(r.stdout)
            return selected - {n for mode,n in todo.items() if mode in proven}
    except (OSError,ValueError,subprocess.TimeoutExpired):
        pass
    return selected


def verify(kind, modes, rows):
    # Use the real helper, reference database views, data-frame assertions and
    # CSV coercions from the organizer's pin; the worker receives pickled inputs.
    pin = PIN if (PIN/"sqlglot/__init__.py").is_file() else ROOT/"cache/pinned"
    sys.path.insert(0, str(pin))
    from sqlglot import parse_one
    from tests import test_executor as suite
    cls = suite.TestExecutor
    cls.setUpClass()
    tpch = kind == "tpch"
    case = cls("runTest")
    try:
        setattr(case, kind+"_sqls", rows)
        if tpch:
            suite.initializer(suite.TPCH_SCHEMA, cls.tpch_tables)
        else:
            suite.initializer(suite.TPCDS_SCHEMA, cls.tpcds_tables)
        done = []
        for mode in modes:
            try:
                case.cache = {}
                if mode == "optimized":
                    if not tpch:
                        continue
                    case.test_optimized_tpch()
                elif mode == "execute":
                    for index, (meta, sql, _) in enumerate(rows):
                        expr, meta = pickle.loads(pickle.dumps((parse_one(sql), meta)))
                        table = pickle.loads(pickle.dumps(suite.mp_execute(expr, meta)))
                        if table is not None:
                            case.subtestHelper(index, table, tpch=tpch)
                else:
                    continue
                done.append(mode)
            except Exception:
                continue
        return done
    finally:
        cls.tearDownClass()


if __name__ == "__main__":
    print(json.dumps(verify(*json.load(sys.stdin))))
