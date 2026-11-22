"""Return only catalogued pytest items which passed all reported phases."""
import json
from pathlib import Path
import sys


class Completed:
    def __init__(self, requested):
        self.requested = set(requested)
        self.items = {}
        self.phases = {}
        self.bad = set()
        self.collection_error = False

    def pytest_collection_finish(self, session):
        for item in session.items:
            base = item.nodeid.split("[", 1)[0]
            if base in self.requested:
                self.items.setdefault(base, set()).add(item.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.collection_error = True

    def pytest_runtest_logreport(self, report):
        node = report.nodeid
        if not report.passed or hasattr(report, "wasxfail"):
            self.bad.add(node)
        if report.passed and type(report).__name__ != "SubtestReport":
            self.phases.setdefault(node, set()).add(report.when)

    def green(self):
        if self.collection_error:
            return []
        return [name for name, rows in self.items.items()
                if rows and all(n not in self.bad and self.phases.get(n) == {"setup", "call", "teardown"}
                                for n in rows)]


def main():
    import os
    import pytest
    folder, request, response = map(Path, sys.argv[1:])
    os.chdir(folder)
    sys.path.insert(0, str(folder))
    names = json.loads(request.read_bytes())
    result = Completed(names)
    code = pytest.main(["-q", "--tb=no", "--continue-on-collection-errors", "-p", "no:cacheprovider",
                        "--rootdir", str(folder), *names], plugins=[result])
    if int(code) in (0, 1):
        response.write_text(json.dumps(result.green()))


if __name__ == "__main__":
    main()
