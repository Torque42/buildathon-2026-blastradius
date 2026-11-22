"""Attribute raw file reads and Python code inspection to the original large parse calls."""

import gzip
import marshal
import os
from pathlib import Path
import sys
import types
import unittest


def main():
    pin, target = map(lambda s: Path(s).resolve(), sys.argv[1:])
    sys.path.insert(0, str(pin))
    os.chdir(pin)
    from tests import test_parser
    original = test_parser.parse_one
    calls = []
    active = False
    busy = False
    opened, inspected = set(), set()
    prefix = str(pin) + "/"

    def audit(event, args):
        nonlocal busy
        if not active or busy:
            return
        busy = True
        try:
            if event == "open" and isinstance(args[0], (str, bytes)):
                path = os.path.abspath(os.fsdecode(args[0]))
                if path.startswith(prefix):
                    opened.add(path[len(prefix):])
            elif (event == "object.__getattr__" and args[1] == "__code__" and
                  isinstance(args[0], types.FunctionType)):
                code = args[0].__code__
                if code.co_filename.startswith(prefix):
                    inspected.add(code.co_filename[len(prefix):])
        finally:
            busy = False

    def wrap(sql, *args, **kwargs):
        nonlocal active
        calls.append(len(sql))
        active = len(sql) >= 100000
        try:
            return original(sql, *args, **kwargs)
        finally:
            active = False
    sys.addaudithook(audit)
    test_parser.parse_one = wrap
    r = unittest.TestResult()
    unittest.TestSuite([test_parser.TestParser("test_max_nodes")]).run(r)
    if not r.wasSuccessful() or r.testsRun != 1 or r.skipped or len(calls)!=6 or sum(n>=100000 for n in calls)!=2:
        raise RuntimeError("Incomplete parser inspection trace")
    target.write_bytes(gzip.compress(marshal.dumps((sys.version_info[:2], sorted(opened),sorted(inspected))),mtime=0))
    print("raw/inspected", opened, inspected, file=sys.stderr)


if __name__ == "__main__":
    main()
