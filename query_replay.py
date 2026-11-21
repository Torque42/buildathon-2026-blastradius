"""Replay affected query stages against the change under a per-patch budget."""
from functools import lru_cache
import gzip
import hashlib
import json
import marshal
import pickle
import subprocess
import sys
import time
from pathlib import Path

HERE=Path(__file__).resolve().parent
MAPS=HERE/"maps"
IDS={kind:"tests/test_executor.py::TestExecutor::test_execute_"+kind for kind in ("tpcds","tpch")}

@lru_cache(1)
def profile():
    return marshal.loads(gzip.decompress((MAPS/"query-traces.gz").read_bytes()))

def refine(selector,text,selected):
    from replay_edit import possible_edit
    choices={k:selector.NUM[n] for k,n in IDS.items() if n in selector.NUM and selector.NUM[n] in selected}
    if not choices:return selected
    edit=possible_edit(selector,text)
    if not edit:return selected
    path,new,mask=edit
    init,sets,data=profile()
    if path in init and sets[init[path]]&mask:return selected
    # Check the time that would actually disappear from the worker schedule.
    possible=(selector.worker_cost(selector.split(selected))-selector.worker_cost(selector.split(selected-set(choices.values()))))/1000
    if possible<0.8:return selected
    queries={}
    for key,n in choices.items():
        work=[]
        for i,case in enumerate(data[key]):
            stages=[stage for stage in ("parse","prepare","optimize","finish","plan","execute_plan","reference")
                    if path in case.get(stage,{}) and sets[case[stage][path]]&mask]
            if stages:work.append((i,stages))
        queries[key]=work
    safe={choices[key] for key,work in queries.items() if not work}
    needed={key:work for key,work in queries.items() if work}
    if needed:
        budget=min(5.0,possible*0.42)
        try:
            result=subprocess.run([sys.executable,"-I",str(HERE/"query_replay.py")],input=json.dumps([path,new,needed,budget]).encode(),capture_output=True,timeout=budget*2+1)
            output=result.stdout
        except subprocess.TimeoutExpired as exc:
            output=exc.stdout or b""
        safe.update(choices[k] for k in output.decode(errors="replace").splitlines() if k in needed)
    return selected-safe


def restore(label):
    return pickle.loads(gzip.decompress((MAPS/"query-inputs"/(label+".gz")).read_bytes()))


@lru_cache(2)
def tables(kind):
    from sqlglot.executor.table import Table
    schema,rows=pickle.loads(gzip.decompress((MAPS/("tables-"+kind+".gz")).read_bytes()))
    return schema,{name:Table(cols,values) for name,(cols,values) in rows.items()}


def verify_query(kind,case,stages):
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
        if expr is None:expr=restore(case["parse_input"])
        schema,_=tables(kind)
        optimized=optimize(expr,ensure_schema(schema),leave_tables_isolated=True,dialect=None)
        downstream|=digest(optimized,())!=case["optimized"]
    if not downstream and not prepare:
        if "finish" in stages:
            if optimized is None:optimized=restore(case["optimized_input"])
            # The pinned executor evaluates this log argument even when debug is off.
            optimized.sql(pretty=True)
        return True
    if expr is None:expr=restore(case["parse_input"])
    schema,all_tables=tables(kind)
    chosen={t.name:all_tables[t.name] for t in find_tables(expr)}
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
    path,new,work,budget=json.load(sys.stdin)
    root=HERE/"cache"/"pinned"
    sys.path[:0]=[str(HERE),str(root)]
    from replay_edit import Overlay
    sys.meta_path.insert(0,Overlay(path,new,root))
    _,_,data=profile()
    end=time.process_time()+budget
    for kind,rows in work.items():
        for i,stages in rows:
            if time.process_time()>end:return
            try:
                if not verify_query(kind,data[kind][i],stages):break
            except Exception:
                break
        else:
            print(kind,flush=True)

if __name__=="__main__":
    run()
