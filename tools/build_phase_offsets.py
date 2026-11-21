"""Record pristine bytecode reachability inside each executor assertion and optimizer rule."""
import gzip
import hashlib
import inspect
import marshal
import os
import pickle
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2])
sys.path.insert(0, str(root))
prefix = str(root) + "/sqlglot/"
monitor = sys.monitoring
slot = 4
events = monitor.events
records = {}
codes = {}
current = None

def enter(code, offset):
    if code.co_filename.startswith(prefix):
        identity = (code.co_filename[len(str(root))+1:], code.co_qualname, code.co_firstlineno,
                    hashlib.blake2s(code.co_code, digest_size=12).digest())
        codes[id(code)] = (code, identity)
        monitor.set_local_events(slot, code, events.INSTRUCTION)
    return monitor.DISABLE

def instruction(code, offset):
    identity = codes[id(code)][1]
    if current is not None:
        if identity not in current:
            current[identity] = bytearray((len(code.co_code) // 2 + 7) // 8)
        index = offset // 2
        current[identity][index >> 3] |= 1 << (index & 7)
    return monitor.DISABLE

def phase(label):
    global current
    if os.environ.get("BR_VALUE_PHASE"):
        import sitecustomize
        sitecustomize.active=label
        return
    current = records.setdefault(label, {}) if label is not None else None
    monitor.restart_events()

monitor.use_tool_id(slot, "query-stage-offsets")
monitor.register_callback(slot, events.PY_START, enter)
monitor.register_callback(slot, events.INSTRUCTION, instruction)
if not os.environ.get("BR_VALUE_PHASE"):
    monitor.set_events(slot, events.PY_START)
phase(("init",))
from tests import test_executor as suite
from sqlglot import parse_one, transpile, executor
suite.TestExecutor.setUpClass()
original_optimize = executor.optimize
plan = executor.Plan
execute_plan = executor.PythonExecutor.execute
state = {}

def mark(stage):
    phase((state["kind"], state["query"], stage))

def call_rule(rule, expr, kwargs):
    index = state["rule"]
    state["rule"] = index + 1
    mark("rule-" + str(index))
    try:
        return rule(expr, **kwargs)
    finally:
        mark("outer")

source = inspect.getsource(original_optimize)
needle = "optimized = rule(optimized, **rule_kwargs)"
assert source.count(needle) == 1
namespace = dict(original_optimize.__globals__, call_rule=call_rule)
exec(compile("from __future__ import annotations\n" + source.replace(needle, "optimized = call_rule(rule, optimized, rule_kwargs)"),
             "<instrumented-optimizer>", "exec"), namespace)
instrumented = namespace["optimize"]

def optimize(*args, **kwargs):
    state["rule"] = 0
    mark("outer")
    result = instrumented(*args, **kwargs)
    mark("finish")
    return result

def setup_plan(*args, **kwargs):
    mark("plan")
    return plan(*args, **kwargs)

def execute(self, *args, **kwargs):
    mark("execute_plan")
    return execute_plan(self, *args, **kwargs)

executor.optimize = optimize
executor.Plan = setup_plan
executor.PythonExecutor.execute = execute
for kind in ("tpch", "tpcds"):
    phase(("init",))
    cls = suite.TestExecutor
    rows = getattr(cls, kind + "_sqls")
    suite.initializer(getattr(suite, kind.upper() + "_SCHEMA"), getattr(cls, kind + "_tables"))
    for index, (opts, sql, _) in enumerate(rows):
        state.update(kind=kind, query=index)
        mark("parse")
        expr = pickle.loads(pickle.dumps(parse_one(sql)))
        if opts.get("execute"):
            mark("prepare")
            table = suite.mp_execute(expr, opts)
            # Account for the process boundary in the original executor tests.
            pickle.loads(pickle.dumps(table))
            mark("reference")
            transpile(sql, write="duckdb")
        phase(None)
phase(("init",))
suite.TestExecutor.tearDownClass()
monitor.set_events(slot, 0)
labels = sorted(records)
by_member = {}
for i, label in enumerate(labels):
    for member, visits in records[label].items():
        groups = by_member.setdefault(member, {})
        offsets = int.from_bytes(visits, "little") | 1
        groups[offsets] = groups.get(offsets, 0) | (1 << i)
destination.write_bytes(gzip.compress(marshal.dumps((labels, by_member)), mtime=0))
print(len(labels), len(by_member), destination.stat().st_size)
