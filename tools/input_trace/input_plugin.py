import json
import os
from pathlib import Path
import sitecustomize as trace
passed = set()

def pytest_runtest_call(item):
    trace.active = item.nodeid.split("[", 1)[0]

def pytest_runtest_teardown(item):
    trace.active = "*"

def pytest_runtest_logreport(report):
    if report.when == "call" and report.passed:
        passed.add(report.nodeid.split("[", 1)[0])

def pytest_sessionfinish():
    Path(os.environ["BR_PROBE_OUT"], "catalog").write_text(json.dumps(sorted(passed)))
