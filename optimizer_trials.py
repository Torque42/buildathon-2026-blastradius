"""Check original optimizer tests and the SQL they send across the database boundary."""

from collections import Counter
from concurrent.futures import Future
from functools import lru_cache, wraps
import ast
import hashlib
import marshal
import os
from pathlib import Path
import pickle
import signal
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest

import replay_edit
import shortlist
import workload_replay as workload

ROOT = Path(__file__).resolve().parent
NODE = "tests/test_optimizer.py::TestOptimizer::"
spent = 0.0


class SQLResult:
    def __init__(self, text):
        self.text = text

    def df(self):
        return self

    def __bool__(self):
        raise TypeError("Database results must reach the original dataframe assertion")


class Boundary:
    def __init__(self):
        self.clear()

    def clear(self):
        self.setup = []
        self.queries = Counter()
        self.comparisons = Counter()
        self.assertions = Counter()
        self.calls = 0
        self.unavailable = False

    def connect(self):
        if sys._getframe(1).f_code.co_name != "setUpClass":
            raise TypeError("Only the pinned class database is supported")
        return self

    def execute(self, sql):
        if type(sql) is not str:
            raise TypeError("Unrecognized statement")
        frame = sys._getframe(1)
        if frame.f_globals.get("__name__") != "tests.test_optimizer":
            raise TypeError("Unrecognized database consumer")
        digest = hashlib.sha256(sql.encode()).digest()
        if frame.f_code.co_name == "setUpClass":
            self.setup.append(digest)
        elif frame.f_code.co_name == "check_file":
            self.queries[digest] += 1
        else:
            self.unavailable = True
            raise TypeError("Direct database consumers need their real rows")
        return SQLResult(digest)

    def compare(self, a, b, *args, **kwargs):
        if type(a) is not SQLResult or type(b) is not SQLResult or args or kwargs:
            self.unavailable = True
            raise TypeError("Unsupported comparison at the database boundary")
        self.comparisons[a.text,b.text] += 1

    def evidence(self):
        if (self.unavailable or not self.assertions or not self.setup or
                sum(self.queries.values()) != 2*sum(self.comparisons.values())):
            return
        return (tuple(self.setup), tuple(sorted(self.queries.items())), tuple(sorted(self.comparisons.items())),
                tuple(sorted(self.assertions.items())), self.calls)


def load(root, boundary):
    testfile = root / "tests/test_optimizer.py"
    import tests
    tree = ast.parse(testfile.read_bytes())
    dropped = []
    for stmt in tree.body:
        if isinstance(stmt,ast.Import) and len(stmt.names)==1 and stmt.names[0].name=="duckdb":
            dropped.append(stmt)
        if isinstance(stmt,ast.ImportFrom) and stmt.module=="pandas.testing" and len(stmt.names)==1 and (
                stmt.names[0].name=="assert_frame_equal"):
            dropped.append(stmt)
    if len(dropped)!=2:
        raise ValueError("Unsupported test module imports")
    tree.body=[s for s in tree.body if s not in dropped]
    module=ModuleType("tests.test_optimizer")
    module.__file__=str(testfile)
    module.__package__="tests"
    sys.modules[module.__name__]=module
    tests.test_optimizer=module
    module.duckdb=SimpleNamespace(connect=boundary.connect)
    module.assert_frame_equal=boundary.compare
    exec(compile(tree,str(testfile),"exec",dont_inherit=True),module.__dict__)

    class LocalPool:
        def __init__(self, **opts):
            if set(opts)!={"mp_context"} or opts["mp_context"].get_start_method()!="spawn":
                raise ValueError("Unsupported executor")

        def submit(self, entry, *args, **kwargs):
            if entry is not module.parse_and_optimize:
                boundary.unavailable=True
                raise TypeError("Unsupported process call")
            boundary.calls += 1
            future=Future()
            try:
                func,values,kw=pickle.loads(pickle.dumps((entry,args,kwargs)))
                result=func(*values,**kw)
                future.set_result(pickle.loads(pickle.dumps(result)))
            except Exception as exc:
                future.set_exception(exc)
            return future

        def shutdown(self):
            pass

    module.ProcessPoolExecutor=LocalPool
    for name,original in list(vars(unittest.TestCase).items()):
        if not callable(original) or not (name.startswith("assert") or name=="subTest"):
            continue
        def wrap(method,op):
            @wraps(method)
            def observed(self,*args,**kwargs):
                caller=sys._getframe(1)
                if caller.f_globals.get("__name__")=="tests.test_optimizer":
                    boundary.assertions[caller.f_code.co_name,caller.f_lineno,op] += 1
                return method(self,*args,**kwargs)
            return observed
        setattr(unittest.TestCase,name,wrap(original,name))
    return module


def execute(module,boundary,name):
    boundary.clear()
    result=unittest.TestResult()
    unittest.TestSuite([module.TestOptimizer(name)]).run(result)
    if (result.testsRun!=1 or not result.wasSuccessful() or result.skipped or result.expectedFailures
            or not any(op.startswith("assert") for _,_,op in boundary.assertions)):
        return
    return boundary.evidence()


@lru_cache(1)
def observations():
    cache=ROOT/"cache/optimizer-trials"
    return marshal.loads(cache.read_bytes()) if cache.is_file() else {}


def tree_edits(selector,text):
    result={}
    for f in selector.parse_patch(text):
        path=f["old"]
        if not path or path!=f["new"] or path in result or ".." in Path(path).parts:
            return
        if path in selector.SRC and path.startswith("sqlglot/"):
            original=selector.SRC[path]
        elif path.startswith("tests/fixtures/") and (shortlist.PIN/path).is_file():
            original=(shortlist.PIN/path).read_text()
        else:
            return
        lines=original.splitlines()
        for h in f["hunks"]:
            old=[x[1:].rstrip("\r") for x in h["lines"] if x.startswith((" ","-"))]
            offset=max(0,h["os"]-1)
            if lines[offset:offset+len(old)]!=old:
                return
        new,_,_=selector.apply(original,f["hunks"])
        result[path]=new
        if path.endswith(".py"):
            # The replay substitutes the worker queue. Retain edits that could couple workers.
            changed=[line for h in f["hunks"] for line in h["lines"] if line.startswith(("+","-"))]
            sensitive=("global ","setattr","delattr","__del__","environ","getpid","fork","thread",
                       "random","time.","sys.","globals(","locals(","eval(","exec(","lru_cache","cache_clear",
                       "multiprocessing","ProcessPool","sqlglot.schema","mock.","inspect.")
            if any(word in line for word in sensitive for line in changed):
                return
    return result if 1<=len(result)<=8 else None


def trial(edits, work, budget):
    with tempfile.TemporaryDirectory(prefix="br-optimizer-") as tmp:
        folder=Path(tmp)
        shortlist.patched_tree(folder,edits)
        rd,wr=os.pipe()
        child=os.fork()
        if child==0:
            try:
                os.close(rd)
                signal.signal(signal.SIGPROF,signal.SIG_DFL)
                signal.signal(signal.SIGALRM,signal.SIG_DFL)
                signal.setitimer(signal.ITIMER_PROF,budget)
                signal.setitimer(signal.ITIMER_REAL,2*budget+1)
                os.chdir(folder)
                sys.path.insert(0,str(folder))
                sys.dont_write_bytecode=True
                os.environ["SKIP_INTEGRATION"]="0"
                for path,source in edits.items():
                    if path.endswith(".py"):
                        sys.meta_path.insert(0,replay_edit.Overlay(path,source,folder))
                with open(os.devnull,"wb") as sink:
                    os.dup2(sink.fileno(),1)
                    os.dup2(sink.fileno(),2)
                    boundary=Boundary()
                    module=load(folder,boundary)
                    for n,name in work:
                        try:
                            if execute(module,boundary,name)==observations()[name][1]:
                                os.write(wr,(str(n)+"\n").encode())
                        except Exception:
                            continue
            finally:
                os._exit(0)
        os.close(wr)
        with os.fdopen(rd,"rb") as out:
            passed={int(line) for line in out}
        os.waitpid(child,0)
        return passed & {n for n,_ in work}


def refine(selector,text,selected):
    global spent
    if not selected or spent>=11 or "sqlglot" in sys.modules:
        return selected
    data=observations()
    choices={n:selector.TEST_FILE_ID[n].removeprefix(NODE) for n in selected if selector.TEST_FILE_ID[n].startswith(NODE)}
    choices={n:name for n,name in choices.items() if name in data}
    if not choices:
        return selected
    base=selector.worker_cost(selector.split(selected))
    maxgain=(base-selector.worker_cost(selector.split(selected-choices.keys())))/1000
    if maxgain < 0.55:
        return selected
    best=None
    for order in (sorted(choices,key=lambda n:-selector.DUR[n]),sorted(choices,key=lambda n:-selector.DUR[n]/(0.001+data[choices[n]][0]))):
        for size in (1,3,8,18,40,len(order)):
            ns=order[:size]
            measured=sum(data[choices[n]][0] for n in ns)
            if measured>3.2:
                continue
            gain=(base-selector.worker_cost(selector.split(selected-set(ns))))/1000
            benefit=gain-1.1*measured-0.12
            if benefit>0.3 and (best is None or benefit>best[0]):
                best=(benefit,gain,measured,ns)
    if best is None:
        return selected
    _,gain,measured,ns=best
    start=workload.cpu()
    try:
        edits=tree_edits(selector,text)
        if edits:
            budget=min(11-spent, 3.7, gain*0.64, max(0.30,1.6*measured+0.20))
            passed=trial(edits,[(n,choices[n]) for n in ns],budget)
            if passed:
                print("verified original optimizer tests",len(passed),file=sys.stderr)
            return selected-passed
    except (OSError,ValueError,TypeError,SyntaxError,KeyError,IndexError):
        pass
    finally:
        spent+=workload.cpu()-start
    return selected
