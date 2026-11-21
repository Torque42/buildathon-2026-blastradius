import gzip,json,os,sys
from pathlib import Path
import keytrace
keytrace.install()
certified=set()
def pytest_collection_modifyitems(config,items):
    pools=set(json.loads(gzip.decompress(Path(os.environ['POOL_MAP']).read_bytes()))['tests'])
    keep=[i for i in items if i.nodeid not in pools];items[:]=keep
def pytest_runtest_setup(item):
    keytrace.active=item.nodeid
def pytest_runtest_logreport(report):
    if report.when=='call' and report.passed:
        certified.add(report.nodeid)
def pytest_runtest_logfinish(nodeid,location):
    keytrace.active=None
def pytest_sessionfinish(session,exitstatus):
    out={'tests':sorted(certified),'data':{k:{(t or ''):list(s) for t,s in keytrace.value_reads[k].items()} for k in sorted(keytrace.created)}}
    Path(os.environ['KEY_RESULTS']).write_bytes(gzip.compress(json.dumps(out,separators=(',',':')).encode(),mtime=0))
