"""Record persistent pytest fixture scopes that read repository data files.

Run from the pinned checkout with its test dependencies installed:
PYTHONPATH=/path/to/selector/tools BLAST_FIXTURE_OUTPUT=/path/to/maps/fixture_scopes.json \
    python -m pytest tests -p build_fixture_scopes
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import pytest


class Recorder:
    def __init__(self, root):
        self.root = str(root) + os.sep
        self.scopes = []
        self.owners = defaultdict(set)
        self.source_lines = defaultdict(lambda: defaultdict(set))

    def trace_calls(self, frame, event, arg):
        filename = frame.f_code.co_filename
        if event == "call" and filename.startswith(self.root + "sqlglot/"):
            return self.trace_lines

    def trace_lines(self, frame, event, arg):
        if event == "line":
            path = frame.f_code.co_filename[len(self.root):]
            for scope in self.scopes:
                self.source_lines[scope][path].add(frame.f_lineno)
        return self.trace_lines

    def audit(self, event, args):
        if event != "open" or not self.scopes:
            return
        path, _, flags = args
        if not isinstance(path, str) or flags & (os.O_WRONLY | os.O_RDWR):
            return
        path = os.path.abspath(path)
        if path.startswith(self.root):
            relative = path[len(self.root):]
            if relative.startswith("tests/fixtures/"):
                self.owners[relative].update(self.scopes)


def pytest_sessionstart(session):
    global recorder
    recorder = Recorder(Path(session.config.rootpath).resolve())
    sys.addaudithook(recorder.audit)


@pytest.hookimpl(wrapper=True)
def pytest_fixture_setup(fixturedef, request):
    if fixturedef.scope == "function":
        return (yield)
    previous = sys.gettrace()
    if not recorder.scopes:
        sys.settrace(recorder.trace_calls)
    recorder.scopes.append(request.node.nodeid)
    try:
        return (yield)
    finally:
        recorder.scopes.pop()
        if not recorder.scopes:
            sys.settrace(previous)


def pytest_sessionfinish(session, exitstatus):
    if exitstatus == 0:
        data = {path: sorted(owners) for path, owners in recorder.owners.items()}
        output = Path(os.environ["BLAST_FIXTURE_OUTPUT"])
        output.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")
        lines = {
            scope: {path: sorted(numbers) for path, numbers in files.items()}
            for scope, files in recorder.source_lines.items()
        }
        output.with_name("fixture_lines.json").write_text(json.dumps(lines, sort_keys=True) + "\n")
