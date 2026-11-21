"""Attribute ordinary pytest calls while keeping setup and collection as unknown."""
import gzip,json,os
from pathlib import Path
import pytest
import sitecustomize as trace
out=Path(os.environ["BR_OUT"])
manifest=json.loads(Path(os.environ["BR_TESTS"]).read_bytes())["tests"]
numbers={t["id"]:t["number"] for t in manifest}
excluded=set(json.loads(os.environ["BR_EXCLUDE"]))
pool={}
unknown_seen=trace.reached
unknown_cmp=trace.comparisons
def pytest_collection_modifyitems(items):
    items[:]=[i for i in items if i.nodeid not in excluded]
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    node=item.nodeid.split("[")[0]
    reached,cmp=pool.setdefault(node,(set(),{}))
    trace.reached,trace.comparisons=reached,cmp
    try:yield
    finally:trace.reached,trace.comparisons=unknown_seen,unknown_cmp
def pytest_sessionfinish(session,exitstatus):
    if exitstatus:return
    for node,(seen,calls) in pool.items():
        if node not in numbers:continue
        path=out/(str(numbers[node])+".partial.gz")
        path.write_bytes(gzip.compress(json.dumps([node,list(seen),{k:[list(a),list(b)] for k,(a,b) in calls.items()}]).encode(),compresslevel=1,mtime=0))
