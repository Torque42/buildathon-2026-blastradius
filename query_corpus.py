"""Run complete raw TPC queries when the normal trace cannot certify a test."""
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import pickle
import re
import signal
import sys

import query_replay
import workload_replay

spent=0.0
FIELDS=("parse","prepare","optimize","finish","plan","execute_plan","reference")
EXCLUDED=workload_replay.PERSISTENT_OPERATIONS|{"__subclasses__","__getattr__","__getattribute__",
    "__setattr__","__delattr__","__new__","__init_subclass__","__class__","__dict__","global",
    "nonlocal","threading","multiprocessing","pickle","atexit","warnings","time","setdefaultencoding",
    "stdout","stderr","__import__","getframe","excepthook"}


def eligible(path):
    return (path in ("sqlglot/parser.py","sqlglot/generator.py","sqlglot/planner.py",
                     "sqlglot/tokenizer_core.py","sqlglot/tokens.py","sqlglot/schema.py")
            or path.startswith(("sqlglot/optimizer/","sqlglot/parsers/","sqlglot/generators/","sqlglot/dialects/")))


def describe(selector,text):
    files=selector.parse_patch(text)
    if not files or len(files)>10 or any(not eligible(p["old"] or "") or p["new"]!=p["old"]
                                           or p["old"] not in selector.SRC for p in files):
        return
    changed=set(re.findall(r"\w+","\n".join(line for p in files for h in p["hunks"] for line in h["lines"]
                                           if line.startswith(("-","+")))))
    if changed&EXCLUDED:
        return
    prepared=workload_replay.prepare(selector,text)
    if not prepared:
        return
    edits,details,opaque,fixtures=prepared
    if fixtures:
        return
    if opaque:
        from namespace_proof import declarations_only
        if any(not declarations_only(selector.SRC[p],edits[p]) for p in opaque):
            return
    return edits,details,opaque


@lru_cache(1)
def catalog():
    return query_replay.profile()[2]


def fixture(kind):
    from tests.helpers import load_sql_fixture_pairs
    label="tpc-ds" if kind=="tpcds" else "tpc-h"
    rows=list(load_sql_fixture_pairs("optimizer/"+label+"/"+label+".sql"))
    cases=catalog()[kind]
    if len(rows)!=len(cases):
        return
    if all(c["sql"]==sql and c["execute"]==bool(meta.get("execute")) for (meta,sql,_),c in zip(rows,cases)):
        return rows


def full_case(kind,case):
    from sqlglot import executor,find_tables,parse_one,transpile
    expr=pickle.loads(pickle.dumps(parse_one(case["sql"])))
    if not case["execute"]:
        return True
    chosen={t.name:query_replay.table_for(kind,t.name) for t in find_tables(expr)}
    result=executor.execute(expr,schema=query_replay.schema_for(kind),tables=chosen)
    wire=pickle.loads(pickle.dumps(result))
    actual=hashlib.sha256(repr((tuple(wire.columns),wire.rows)).encode()).hexdigest()
    return actual==case["table"] and transpile(case["sql"],write="duckdb")[0]==case["duck"]


def execute(edits,full,order):
    import unittest
    import replay_edit
    import snapshot_cache
    root=query_replay.HERE/"cache/pinned"
    sys.path.insert(0,str(root))
    sources=[(p,s,0) for p,s in edits.items()]
    for p,s in edits.items():
        sys.meta_path.insert(0,replay_edit.Overlay(p,s,root))
    # Resolve the production imports that the original test module binds.
    from sqlglot import exp,find_tables,parse_one,transpile
    from sqlglot.errors import ExecuteError
    from sqlglot.executor import env,execute as sql_execute
    from sqlglot.executor.python import Python,PythonExecutor
    from sqlglot.executor.table import Table,ensure_tables
    from sqlglot.optimizer import optimize
    from sqlglot.planner import Plan
    from tests.helpers import TPCH_SCHEMA,TPCDS_SCHEMA
    import row_programs,row_loops
    snapshot_cache.configure(sources)
    if not full:
        row_programs.install(sources)
        row_loops.install(sources)
    for kind in order:
        if fixture(kind) is None or query_replay.schema_for(kind)!=(TPCH_SCHEMA if kind=="tpch" else TPCDS_SCHEMA):
            continue
        cases=catalog()[kind]
        for case in cases:
            if kind in full:
                if not full_case(kind,case):
                    break
            else:
                # Function bodies retain their namespace initialization. Use
                # the entire changed file as the dependency for every stage.
                stages=[stage for stage in FIELDS if any(p in case.get(stage,{}) for p in edits)]
                if stages and not query_replay.verify_query(kind,case,stages,sources,True,None):
                    break
        else:
            yield kind


def checked(edits,full,order,budget):
    if not hasattr(os,"fork") or any(n=="sqlglot" or n.startswith("sqlglot.") for n in sys.modules):
        return set()
    rd,wr=os.pipe()
    try:
        p=os.fork()
    except OSError:
        os.close(rd)
        os.close(wr)
        return set()
    if not p:
        try:
            os.close(rd)
            signal.signal(signal.SIGPROF,signal.SIG_DFL)
            signal.signal(signal.SIGALRM,signal.SIG_DFL)
            signal.setitimer(signal.ITIMER_PROF,budget)
            signal.setitimer(signal.ITIMER_REAL,2*budget+1)
            with open(os.devnull,"wb") as sink:
                os.dup2(sink.fileno(),1)
                os.dup2(sink.fileno(),2)
            for kind in execute(edits,full,order):
                os.write(wr,(kind+"\n").encode())
        finally:
            os._exit(0)
    os.close(wr)
    try:
        with os.fdopen(rd,"rb") as f:
            done=set(f.read(40).decode().splitlines())
    finally:
        os.waitpid(p,0)
    return done & set(order)


def refine(selector,text,selected):
    global spent
    targets={kind:selector.NUM[node] for kind,node in query_replay.IDS.items() if selector.NUM.get(node) in selected}
    if not targets or spent>=18:
        return selected
    gain=(selector.worker_cost(selector.split(selected))-
          selector.worker_cost(selector.split(selected-set(targets.values()))))/1000
    if gain<2.0:
        return selected
    start=workload_replay.cpu()
    try:
        ready=describe(selector,text)
        if not ready:
            return selected
        edits,details,opaque=ready
        protect=workload_replay.file_readers(selector,edits)
        for k in tuple(targets):
            if targets[k] in protect:
                del targets[k]
        if not targets:
            return selected
        full=set(targets) if opaque else set()
        if not opaque:
            from query_phases import profile
            labels,trace,kinds=profile()
            for k in targets:
                mask=workload_replay.affected_stages(query_replay.IDS[k],details,set(),trace,kinds[k]|kinds["init"])
                if mask is None or mask&kinds["init"] or any(label[2]=="harness" and mask&(1<<i)
                                                             for i,label in enumerate(labels) if label[0]==k):
                    full.add(k)
        budget=min(9.0,18-spent,gain*0.66)
        order=sorted(targets,key=lambda k:k!="tpch")
        passed=checked(edits,full,order,budget)
        return selected-{targets[k] for k in passed}
    except (KeyError,ValueError,SyntaxError,OSError,TypeError,AttributeError,RecursionError):
        return selected
    finally:
        spent+=workload_replay.cpu()-start
