"""Cross the same process and pickle boundaries as the pinned TPC executor tests."""
from contextlib import contextmanager
import os
import pickle
import signal
import sys


def serve(edits,connection,budget):
    import unittest
    import query_replay as q
    import replay_edit
    root=q.HERE/"cache/pinned"
    sys.path.insert(0,str(root))
    for path,src in edits.items():
        sys.meta_path.insert(0,replay_edit.Overlay(path,src,root))
    from sqlglot import executor,find_tables
    from sqlglot.executor.table import Table
    from tests.helpers import TPCH_SCHEMA,TPCDS_SCHEMA
    import scan_cache
    scan_cache.install([(p,s,0) for p,s in edits.items()])
    # The pinned fixture initializes all tables before accepting the Pool's tasks.
    tables={}
    for kind,schema in (("tpch",TPCH_SCHEMA),("tpcds",TPCDS_SCHEMA)):
        if q.schema_for(kind)!=schema:
            return
        tables[kind]={n:q.table_for(kind,n) for n in schema}
    while True:
        try:
            kind,enabled,packet=connection.recv()
        except EOFError:
            break
        expression=pickle.loads(packet)
        if not enabled:
            output=None
        else:
            chosen={t.name:tables[kind][t.name] for t in find_tables(expression)}
            output=executor.execute(expression,schema=q.schema_for(kind),tables=chosen)
            if type(output) is not Table:
                return
        connection.send_bytes(pickle.dumps(output))


@contextmanager
def session(edits,budget):
    from multiprocessing import Pipe
    master,slave=Pipe()
    pid=os.fork()
    if not pid:
        master.close()
        try:
            signal.signal(signal.SIGPROF,signal.SIG_DFL)
            signal.signal(signal.SIGALRM,signal.SIG_DFL)
            signal.setitimer(signal.ITIMER_PROF,budget)
            signal.setitimer(signal.ITIMER_REAL,2*budget+0.5)
            serve(edits,slave,budget)
        except (Exception,SystemExit):
            pass
        finally:
            slave.close()
            os._exit(0)
    slave.close()
    try:
        yield master
    finally:
        master.close()
        try:
            os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        os.waitpid(pid,0)
