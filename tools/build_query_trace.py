"""Record pristine stages of two expensive parameterized SQL executor tests."""
import functools
import gzip
import hashlib
import json
import marshal
import os
import pickle
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
src=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(src),str(root)]
from replay_state import digest

trace={}
tool=4
monitor=sys.monitoring
prefix=str(src)+"/"
files={}
def enter(code,offset):
    path=code.co_filename
    if path.startswith(prefix+"sqlglot/"):
        files[code]=path[len(prefix):]
        monitor.set_local_events(tool,code,monitor.events.LINE)
    return monitor.DISABLE

def line(code,lineno):
    trace.setdefault(files[code],set()).add(lineno)
    return monitor.DISABLE

def phase():
    global trace
    trace={}
    monitor.restart_events()
    return trace

monitor.use_tool_id(tool,"queries")
monitor.register_callback(tool,monitor.events.PY_START,enter)
monitor.register_callback(tool,monitor.events.LINE,line)
monitor.set_events(tool,monitor.events.PY_START)
init=phase()
from tests import test_executor as suite
from sqlglot import parse_one,transpile
from sqlglot import executor
from sqlglot.executor.table import Table
from sqlglot.serde import dump
suite.TestExecutor.setUpClass()
import inspect
import lzma
chunks={}
records={}
def capture(rule,expression,kwargs):
    # Called at the pinned assignment optimized = rule(optimized, **rule_kwargs).
    # Exclude profiling work from both caller and callee coverage.
    phase()
    raw=pickle.dumps((rule.__module__,rule.__name__,expression,kwargs),protocol=5)
    key=hashlib.sha256(raw).digest()
    chunks[key]=raw
    coverage=phase()
    result=rule(expression,**kwargs)
    phase()
    fingerprint=digest(result,())
    records.setdefault(rule.__module__,set()).add(key)
    case["rules"].append((key,fingerprint,coverage))
    case["outer_opt"].append(phase())
    return result

real_opt=executor.optimize
src_text=inspect.getsource(real_opt)
assert src_text.count("optimized = rule(optimized, **rule_kwargs)")==1
exec(compile("from __future__ import annotations\n"+src_text.replace("optimized = rule(optimized, **rule_kwargs)","optimized = capture(rule,optimized,rule_kwargs)"),"<pinned-optimizer-trace>","exec"),dict(real_opt.__globals__,capture=capture),scope:={})
real_opt=scope["optimize"]
real_plan=executor.Plan
real_execute=executor.PythonExecutor.execute
def traced_plan(*args,**kwargs):
    case["plan"]=phase()
    return real_plan(*args,**kwargs)
def traced_execute(*args,**kwargs):
    case["execute_plan"]=phase()
    return real_execute(*args,**kwargs)
executor.Plan=traced_plan
executor.PythonExecutor.execute=traced_execute
case={}
stage="initial"
def traced_opt(*args,**kwargs):
    case["rules"]=[]
    case["outer_opt"]=[phase()]
    result=real_opt(*args,**kwargs)
    phase()
    case["optimized"]=digest(result,())
    case["optimized_input"]=pickle.dumps(result,protocol=5)
    case["finish"]=phase()
    return result
executor.optimize=traced_opt
sets=[]
unique={}
def pack_coverage(cov):
    d={}
    for path,nums in cov.items():
        bits=sum(1<<n for n in nums)
        if bits not in unique:
            unique[bits]=len(sets);sets.append(bits)
        d[path]=unique[bits]
    return d

def table_digest(t):
    return hashlib.sha256(repr((tuple(t.columns),t.rows)).encode()).hexdigest()

data={}
for name,tpch in [("tpch",True),("tpcds",False)]:
    cls=suite.TestExecutor
    queries=cls.tpch_sqls if tpch else cls.tpcds_sqls
    tables=cls.tpch_tables if tpch else cls.tpcds_tables
    schema=suite.TPCH_SCHEMA if tpch else suite.TPCDS_SCHEMA
    suite.initializer(schema,tables)
    cases=[]
    for opts,sql,_ in queries:
        case={"sql":sql,"execute":bool(opts.get("execute"))}
        case["parse"]=phase()
        expr=pickle.loads(pickle.dumps(parse_one(sql)))
        phase()
        case["parsed"]=digest(expr,())
        case["parse_input"]=pickle.dumps(expr,protocol=5)
        if case["execute"]:
            case["prepare"]=phase()
            table=suite.mp_execute(expr,opts)
            phase()
            case["table"]=table_digest(table)
            case["reference"]=phase()
            case["duck"]=transpile(sql,write="duckdb")[0]
            phase()
        if "rules" in case:
            outer={}
            for frame in case.pop("outer_opt"):
                for f,lines in frame.items():outer.setdefault(f,set()).update(lines)
            case["outer"]=pack_coverage(outer)
            total={f:set(v) for f,v in outer.items()}
            rules=[]
            for key,fp,cov in case["rules"]:
                for f,lines in cov.items():total.setdefault(f,set()).update(lines)
                rules.append((key,fp,pack_coverage(cov)))
            case["rules"]=rules
            case["optimize"]=total
        for key in ("parse","prepare","optimize","finish","plan","execute_plan","reference"):
            if key in case:case[key]=pack_coverage(case[key])
        snap_dir=root/"maps"/"query-inputs"
        snap_dir.mkdir(exist_ok=True)
        for key in ("optimized_input","parse_input"):
            if key in case:
                content=case[key]
                label=hashlib.sha256(content).hexdigest()
                (snap_dir/(label+".gz")).write_bytes(gzip.compress(content,mtime=0))
                case[key]=label
        cases.append(case)
    data[name]=cases
    blob=(schema,{k:(v.columns,v.rows) for k,v in tables.items()})
    (root/"maps"/("tables-"+name+".gz")).write_bytes(gzip.compress(pickle.dumps(blob,protocol=5),mtime=0))
    print(name,len(cases),flush=True)
suite.TestExecutor.tearDownClass()
monitor.set_events(tool,0)
(root/"maps/query-traces.gz").write_bytes(gzip.compress(marshal.dumps((pack_coverage(init),sets,data)),mtime=0))

index={}
with (root/"maps/query-rule-blocks.bin").open("wb") as stream:
    def pack(keys):
        start=stream.tell()
        binary=lzma.compress(marshal.dumps([chunks[k] for k in keys]),preset=2)
        stream.write(binary)
        for i,k in enumerate(keys):index[k]=(start,len(binary),i)
    for module,keys in sorted(records.items()):
        block=[];size=0
        for key in sorted(keys,key=lambda x:(len(chunks[x]),x)):
            if size>750000:
                pack(block);block=[];size=0
            block.append(key);size+=len(chunks[key])
        if block:pack(block)
(root/"maps/query-rule-index.gz").write_bytes(gzip.compress(marshal.dumps(index),mtime=0))
