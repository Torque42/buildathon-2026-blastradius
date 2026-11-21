"""Replay affected query stages against the change under a per-patch budget."""
from functools import lru_cache
import gzip
import hashlib
import json
import marshal
import lzma
import importlib
import pickle
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE=Path(__file__).resolve().parent
MAPS=HERE/"maps"
IDS={kind:"tests/test_executor.py::TestExecutor::test_execute_"+kind for kind in ("tpcds","tpch")}

def read_map(name):
    cached=HERE/"cache"/name
    return marshal.loads(cached.read_bytes() if cached.is_file() else gzip.decompress((MAPS/(name+".gz")).read_bytes()))

@lru_cache(1)
def profile():
    return read_map("query-traces")

def refine(selector,text,selected):
    from replay_edit import possible_edit
    edit=possible_edit(selector,text)
    return refine_edits(selector,[edit],selected) if edit else selected


def refine_edits(selector,edits,selected):
    choices={k:selector.NUM[n] for k,n in IDS.items() if n in selector.NUM and selector.NUM[n] in selected}
    if not choices:return selected
    init,sets,data=profile()
    for path,new,mask in edits:
        if path in {"sqlglot/optimizer/optimizer.py","sqlglot/serde.py","sqlglot/executor/__init__.py"}:return selected
        if path in init and sets[init[path]]&mask:return selected
    # Check the time that would actually disappear from the worker schedule.
    possible=(selector.worker_cost(selector.split(selected))-selector.worker_cost(selector.split(selected-set(choices.values()))))/1000
    if possible<0.8:return selected
    queries={}
    for key,n in choices.items():
        work=[]
        for i,case in enumerate(data[key]):
            stages=[stage for stage in ("parse","prepare","optimize","finish","plan","execute_plan","reference")
                    if any(path in case.get(stage,{}) and sets[case[stage][path]]&mask for path,new,mask in edits)]
            if stages:work.append((i,stages))
        queries[key]=work
    safe={choices[key] for key,work in queries.items() if not work}
    needed={key:work for key,work in queries.items() if work}
    if needed:
        budget=min(5.0,possible*0.42)
        try:
            result=subprocess.run([sys.executable,"-I",str(HERE/"query_replay.py")],input=json.dumps([[(path,new,hex(mask)) for path,new,mask in edits],needed,budget]).encode(),capture_output=True,timeout=budget*2+1)
            output=result.stdout
        except subprocess.TimeoutExpired as exc:
            output=exc.stdout or b""
        safe.update(choices[k] for k in output.decode(errors="replace").splitlines() if k in needed)
    return selected-safe


def restore(label):
    cached=HERE/"cache"/"query-inputs"/label
    return pickle.loads(cached.read_bytes() if cached.is_file() else gzip.decompress((MAPS/"query-inputs"/(label+".gz")).read_bytes()))


@lru_cache(2)
def tables(kind):
    from sqlglot.executor.table import Table
    schema,rows=pickle.loads(gzip.decompress((MAPS/("tables-"+kind+".gz")).read_bytes()))
    return schema,{name:Table(cols,values) for name,(cols,values) in rows.items()}


@lru_cache(2)
def schema_for(kind):
    cached=HERE/"cache"/kind/"schema"
    return pickle.loads(cached.read_bytes()) if cached.is_file() else tables(kind)[0]

@lru_cache(64)
def table_for(kind,name):
    from sqlglot.executor.table import Table
    if not name.replace("_","").isalnum():raise KeyError(name)
    cached=HERE/"cache"/kind/("table-"+name)
    if cached.is_file():return Table(*pickle.loads(cached.read_bytes()))
    return tables(kind)[1][name]

@lru_cache(1)
def rule_index():
    return read_map("query-rule-index")

@lru_cache(10)
def rule_block(start,size):
    with (MAPS/"query-rule-blocks.bin").open("rb") as stream:
        stream.seek(start)
        return marshal.loads(lzma.decompress(stream.read(size)))

def replay_rules(case,edits):
    from replay_state import digest
    _,bits,_=profile()
    changed=None
    for key,expected,coverage in case["rules"]:
        if changed is None and not any(path in coverage and bits[coverage[path]]&mask for path,_,mask in edits):
            continue
        start,size,i=rule_index()[key]
        cached=HERE/"cache"/"rules"/f"{start}-{i}"
        module,name,pristine,kwargs=pickle.loads(cached.read_bytes() if cached.is_file() else rule_block(start,size)[i])
        fn=getattr(importlib.import_module(module),name)
        result=fn(pristine if changed is None else changed,**kwargs)
        changed=None if digest(result,())==expected else result
    return changed

def verify_query(kind,case,stages,edits):
    from sqlglot import parse_one,transpile,find_tables
    from sqlglot import executor
    from sqlglot.optimizer import optimize
    from sqlglot.schema import ensure_schema
    from replay_state import digest
    if "reference" in stages and transpile(case["sql"],write="duckdb")[0]!=case["duck"]:
        return False
    expr=None
    parse_changed=False
    if "parse" in stages:
        expr=pickle.loads(pickle.dumps(parse_one(case["sql"])))
        if case["execute"]:
            parse_changed=digest(expr,())!=case["parsed"]
    if not case["execute"]:
        return True
    prepare="prepare" in stages
    opt_required=parse_changed or "optimize" in stages
    downstream="plan" in stages or "execute_plan" in stages
    if not (prepare or opt_required or downstream or "finish" in stages):
        return True
    optimized=None
    if opt_required and not prepare:
        _,bits,_=profile()
        outer=case.get("outer",{})
        if not parse_changed and all(path.startswith("sqlglot/optimizer/") or not any(path in case.get(st,{}) and bits[case[st][path]]&mask for st in ("optimize",)) for path,_,mask in edits) and "rules" in case and not any(path in outer and bits[outer[path]]&mask for path,_,mask in edits):
            optimized=replay_rules(case,edits)
            downstream|=optimized is not None
        else:
            if expr is None:expr=restore(case["parse_input"])
            schema=schema_for(kind)
            optimized=optimize(expr,ensure_schema(schema),leave_tables_isolated=True,dialect=None)
            downstream|=digest(optimized,())!=case["optimized"]
    if not downstream and not prepare:
        if "finish" in stages:
            if optimized is None:optimized=restore(case["optimized_input"])
            # The pinned executor evaluates this log argument even when debug is off.
            optimized.sql(pretty=True)
        return True
    if expr is None:expr=restore(case["parse_input"])
    schema=schema_for(kind)
    chosen={t.name:table_for(kind,t.name) for t in find_tables(expr)}
    original=executor.optimize
    if not prepare:
        if optimized is None:optimized=restore(case["optimized_input"])
        executor.optimize=lambda *args,**kwargs:optimized
    try:
        out=executor.execute(expr,schema=schema,tables=chosen)
        out=pickle.loads(pickle.dumps(out))
        return hashlib.sha256(repr((tuple(out.columns),out.rows)).encode()).hexdigest()==case["table"]
    finally:
        executor.optimize=original


def run():
    encoded,work,budget=json.load(sys.stdin)
    edits=[(path,new,int(mask,16)) for path,new,mask in encoded]
    root=HERE/"cache"/"pinned"
    sys.path[:0]=[str(HERE),str(root)]
    from replay_edit import Overlay
    for path,new,mask in edits:sys.meta_path.insert(0,Overlay(path,new,root))
    _,_,data=profile()
    signal.signal(signal.SIGPROF,lambda *_:sys.exit(0))
    signal.setitimer(signal.ITIMER_PROF,budget)
    end=time.process_time()+budget
    for kind,rows in work.items():
        for i,stages in rows:
            if time.process_time()>end:return
            try:
                if not verify_query(kind,data[kind][i],stages,edits):break
            except Exception:
                break
        else:
            print(kind,flush=True)

if __name__=="__main__":
    run()
