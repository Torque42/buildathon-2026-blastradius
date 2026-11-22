"""Recheck the real pinned optimizer assertions for independently changed fixture cases."""
import ast
from functools import lru_cache
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PIN = ROOT / "cache/pinned"
MODULE = "tests/test_optimizer.py"


@lru_cache(1)
def declarations():
    module = ast.parse((PIN/MODULE).read_bytes())
    cls = next(x for x in module.body if isinstance(x, ast.ClassDef) and x.name == "TestOptimizer")
    methods = {x.name:x for x in cls.body if isinstance(x, ast.FunctionDef)}
    cases = {}
    for name, node in methods.items():
        if not name.startswith("test_") or node.decorator_list or len(node.body) != 1:
            continue
        stmt = node.body[0]
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) and call.func.attr == "check_file"
                and isinstance(call.func.value, ast.Name) and call.func.value.id == "self" and len(call.args) == 2
                and isinstance(call.args[0], ast.Constant) and type(call.args[0].value) is str):
            continue
        cases.setdefault("tests/fixtures/optimizer/"+call.args[0].value+".sql", []).append(name)
    return module, methods, cases


def refine(selector, text, selected):
    import fixture_inputs
    if not selected or "tests/fixtures/optimizer/" not in text:
        return selected
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return selected
    patch = patches[0]
    path = patch["old"]
    if not path or path != patch["new"] or not path.startswith("tests/fixtures/optimizer/"):
        return selected
    eligible = {m:selector.NUM.get(MODULE + "::TestOptimizer::" + m) for m in declarations()[2].get(path, ())}
    eligible = {m:n for m,n in eligible.items() if n in selected}
    if not eligible:
        return selected
    data, _ = fixture_inputs.profiles()
    if set(data["catalog"]) != set(selector.NUM) or path not in data["contents"]:
        return selected
    old = data["contents"][path]
    old_lines = old.splitlines()
    for h in patch["hunks"]:
        pos = h["os"]-1
        for line in h["lines"]:
            if line.startswith((" ","-")):
                if pos < 0 or pos >= len(old_lines) or line[1:].rstrip("\r") != old_lines[pos]:
                    return selected
                pos += 1
    new = selector.apply(old, patch["hunks"])[0]
    try:
        before, after = fixture_inputs.pairs(old), fixture_inputs.pairs(new)
    except (IndexError, ValueError):
        return selected
    if len(before) != len(after):
        return selected
    rows = [(a,b) for a,b in zip(before,after) if a != b]
    if not 1 <= len(rows) <= 4 or any(a[0] != b[0] or a[1] == b[1] for a,b in rows):
        return selected
    # Abandon trials with too little potential saving or very large statements.
    if sum(len(row[1]) for pair in rows for row in pair) > 24000:
        return selected
    available = (selector.worker_cost(selector.split(selected)) -
                 selector.worker_cost(selector.split(selected-set(eligible.values()))))/1000
    if available < 0.35:
        return selected
    try:
        result = subprocess.run([sys.executable,"-I",str(ROOT/"fixture_assertions.py")],
                                input=json.dumps([path,list(eligible),rows]).encode(),capture_output=True,
                                timeout=min(1.5,available*0.65))
        if result.returncode != 0:
            return selected
        proven = json.loads(result.stdout)
    except (OSError,ValueError,subprocess.TimeoutExpired):
        return selected
    return selected - {eligible[name] for name in proven if name in eligible}


def verify(path, names, rows):
    import pickle
    import unittest
    from concurrent.futures import Future, wait, as_completed
    sys.path[:0] = [str(ROOT),str(PIN)]
    import sqlglot
    from sqlglot import exp, optimizer, parse_one
    from sqlglot.optimizer.scope import build_scope
    from sqlglot.schema import MappingSchema
    from query_replay import schema_for
    mod, methods, allowed = declarations()
    helpers = ast.parse((PIN/"tests/helpers.py").read_bytes())
    namespace = dict(json=json, sys=sys, exp=exp, optimizer=optimizer, parse_one=parse_one,
                     unittest=unittest, build_scope=build_scope, wait=wait, as_completed=as_completed)
    namespace["TPCH_SCHEMA"] = schema_for("tpch")
    namespace["TPCDS_SCHEMA"] = schema_for("tpcds")
    functions = [n for n in mod.body if isinstance(n,ast.FunctionDef) and
                 n.name in ("parse_and_optimize","pushdown_projections","pushdown_ctes")]
    functions += [n for n in helpers.body if isinstance(n,ast.FunctionDef) and n.name=="string_to_bool"]
    functions += [methods["check_file"], *(methods[n] for n in allowed.get(path,()) if n in names)]
    exec(compile(ast.Module(body=functions,type_ignores=[]),str(PIN/MODULE),"exec",dont_inherit=True),namespace)
    setup = methods["setUp"]
    schema = next(ast.literal_eval(s.value) for s in setup.body if isinstance(s,ast.Assign) and
                  len(s.targets)==1 and isinstance(s.targets[0],ast.Attribute) and s.targets[0].attr=="schema")
    class Pool:
        def submit(self, fn, *args, **kwargs):
            future = Future()
            args, kwargs = pickle.loads(pickle.dumps((args[1:],kwargs)))
            # The actual worker checks parent/child links before pickling its return value.
            result = fn(self.rule,*args,**kwargs)
            future.set_result(pickle.loads(pickle.dumps(result)))
            return future

    class SQL:
        def __init__(self): self.queries = []
        def execute(self, sql):
            if type(sql) is not str: raise TypeError
            self.queries.append(sql)
            return self
        def df(self): return self

    class Case(unittest.TestCase):
        def check_file(self, filename, func, **kwargs):
            if "tests/fixtures/optimizer/"+filename+".sql" != path: raise ValueError
            self.pool.rule = func
            return namespace["check_file"](self,filename,func,**kwargs)

    def compare_frames(left, right):
        if left is not right or not isinstance(left, SQL): raise TypeError

    namespace["assert_frame_equal"] = compare_frames
    def run(name, row):
        case = Case()
        case.pool, case.conn = Pool(), SQL()
        case.schema = pickle.loads(pickle.dumps(schema))
        sqlglot.schema = MappingSchema()
        namespace["load_sql_fixture_pairs"] = lambda _file: [row]
        namespace[name](case)
        if len(case.conn.queries) not in (0,2): raise ValueError
        return case.conn.queries

    # Cases without DB assertions must satisfy the actual expected-SQL and tree checks.
    # For a DB assertion both SQL statements must also match the corresponding
    # pristine statements, so the pre-existing database sees the same two reads.
    return [n for n in names if n in allowed.get(path,()) and all(run(n,a)==run(n,b) for a,b in rows)]


if __name__ == "__main__":
    print(json.dumps(verify(*json.load(sys.stdin))))
