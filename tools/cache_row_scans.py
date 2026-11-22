"""Build the original query scan cache and independently replay all original table results."""
from pathlib import Path
import hashlib
import pickle
import sys
r=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(r),str(r/"cache/pinned")]
import query_replay as q
import scan_cache as sc
from sqlglot import executor,find_tables
from sqlglot.executor.python import PythonExecutor
sc.DIRECTORY.mkdir(exist_ok=True)
def exercise():
    count=0
    for kind,cases in q.profile()[2].items():
        for case in cases:
            if not case["execute"]:
                continue
            expr=q.restore(case["parse_input"])
            opt=q.restore(case["optimized_input"])
            tables={t.name:q.table_for(kind,t.name) for t in find_tables(expr)}
            old=executor.optimize
            executor.optimize=lambda *a,**kw:opt
            try:
                table=executor.execute(expr,schema=q.schema_for(kind),tables=tables)
                if hashlib.sha256(repr((tuple(table.columns),table.rows)).encode()).hexdigest()!=case["table"]:
                    raise ValueError("Original result mismatch")
                count+=1
            finally:
                executor.optimize=old
    return count
original=sc.install([],record=True)
n=exercise()
PythonExecutor._project_and_filter=original
sc.install([("sqlglot/parser.py","",0)])
if exercise()!=n or not sc.stats[0] or not sc.stats[1]:
    raise ValueError(("Unverified scan results",sc.stats))
print("Validated",n,"queries, scan writes/hits/misses",sc.stats,
      "unique entries",sum(1 for _ in sc.DIRECTORY.iterdir()),flush=True)
