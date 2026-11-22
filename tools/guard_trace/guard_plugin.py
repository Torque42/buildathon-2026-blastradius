import os
import json
from pathlib import Path

import pytest
import sitecustomize

passed = set()
failed = set()
skipped = set()
collection_errors = set()


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item):
    context = item.nodeid.split("[", 1)[0]
    sitecustomize.current = context
    os.environ["BR_GUARD_TEST"] = context


def pytest_runtest_logfinish(nodeid, location):
    sitecustomize.current = "*"
    os.environ.pop("BR_GUARD_TEST", None)


def pytest_runtest_logreport(report):
    node = report.nodeid.split("[", 1)[0]
    if report.failed:
        failed.add(node)
    if report.skipped:
        skipped.add(node)
    if report.when == "call" and report.passed:
        passed.add(node)


def pytest_collectreport(report):
    if report.failed:
        collection_errors.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    (Path(os.environ["BR_GUARD_OUT"]) / "suite.json").write_text(json.dumps({
        "status": exitstatus, "passed": sorted(passed), "skipped": sorted(skipped),
        "failed": sorted(failed), "collection_errors": sorted(collection_errors)}))
