"""Record separate instruction footprints for each call in the real node-limit test."""

import functools
import gzip
import hashlib
import marshal
import os
from pathlib import Path
import sys
import time
import unittest


def main():
    root, output = map(lambda word: Path(word).resolve(), sys.argv[1:])
    os.chdir(root)
    sys.path.insert(0, str(root))
    prefix = str(root) + "/"
    monitoring = sys.monitoring
    slot = 4
    events = monitoring.events
    stages = {"outside": {}}
    code_keys = {}
    current = "outside"
    calls = []

    def start(code, offset):
        if code.co_filename.startswith(prefix + "sqlglot/"):
            key = (code.co_filename[len(prefix):], code.co_qualname, code.co_firstlineno,
                   hashlib.blake2s(code.co_code, digest_size=12).digest())
            code_keys[id(code)] = (code, key)
            monitoring.set_local_events(slot, code, events.INSTRUCTION)
        return monitoring.DISABLE

    def instruction(code, offset):
        key = code_keys[id(code)][1]
        bucket = stages[current]
        bits = bucket.get(key)
        if bits is None:
            bits = bucket[key] = bytearray((len(code.co_code) // 2 + 7) // 8)
        bit = offset // 2
        bits[bit // 8] |= 1 << (bit % 8)
        return monitoring.DISABLE

    monitoring.use_tool_id(slot, "test-workload")
    monitoring.register_callback(slot, events.PY_START, start)
    monitoring.register_callback(slot, events.INSTRUCTION, instruction)
    monitoring.set_events(slot, events.PY_START)
    from tests import test_parser
    original = test_parser.parse_one

    @functools.wraps(original)
    def observe(sql, *args, **kwargs):
        nonlocal current
        assert isinstance(sql, str) and not args
        key = (hashlib.sha256(sql.encode()).digest(), tuple(sorted(kwargs.items())))
        index = len(calls)
        calls.append(None)
        current = str(index)
        stages[current] = {}
        monitoring.restart_events()
        start_time = time.process_time()
        error = None
        try:
            return original(sql, **kwargs)
        except Exception as exc:
            error = (type(exc).__module__, type(exc).__qualname__, str(exc))
            raise
        finally:
            calls[index] = (key, len(sql), time.process_time() - start_time, error)
            current = "outside"
            monitoring.restart_events()

    test_parser.parse_one = observe
    result = unittest.TestResult()
    unittest.TestSuite([test_parser.TestParser("test_max_nodes")]).run(result)
    monitoring.set_events(slot, 0)
    if not result.wasSuccessful() or result.testsRun != 1 or result.skipped:
        raise RuntimeError((result.failures, result.errors, result.skipped))
    if len(calls) != 6 or len({key for key, _, _, _ in calls}) != len(calls):
        raise RuntimeError("Pinned test invoked unexpected parse calls")
    reads = {phase: {key: int.from_bytes(bits, "little") | 1 for key, bits in bucket.items()}
             for phase, bucket in stages.items()}
    raw = marshal.dumps((sys.version_info[:2], calls, reads))
    output.write_bytes(gzip.compress(raw, compresslevel=6, mtime=0))
    for index, (_, length, cpu, error) in enumerate(calls):
        print(index, length, round(cpu, 3), error[:2] if error else "returned",
              len(reads[str(index)]), file=sys.stderr)


if __name__ == "__main__":
    main()
