"""Track active test files while pytest collects the complete pristine suite."""
import os
from pathlib import Path
import pytest
import sitecustomize as trace

@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector):
    name = str(collector.nodeid).split("::",1)[0]
    previous = trace.active
    if name.endswith(".py"):
        trace.active = name
        trace.monitor.restart_events()
    try:
        yield
    finally:
        if previous != trace.active:
            trace.active = previous
            trace.monitor.restart_events()
