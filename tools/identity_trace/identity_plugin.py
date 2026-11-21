import json
import os
from pathlib import Path
import sitecustomize as trace
import pytest

passed=set()
def pytest_collection_modifyitems(items):
    excluded=set(json.loads(os.environ.get("BR_EXCLUDE","[]")))
    items[:]=[item for item in items if item.nodeid not in excluded]
def pytest_runtest_setup(item):
    trace.active="scope:"+item.nodeid.split("::")[0]
def pytest_runtest_call(item):
    trace.active=item.nodeid
def pytest_runtest_teardown(item):
    trace.active="scope:"+item.nodeid.split("::")[0]
def pytest_runtest_logreport(report):
    if report.when=="call" and report.passed:passed.add(report.nodeid)
def pytest_runtest_logfinish(nodeid,location):
    trace.active=os.environ.get("BR_TEST","")
def pytest_sessionfinish():
    Path(os.environ["BR_OUT"],"passed.json").write_text(json.dumps(sorted(passed)))
