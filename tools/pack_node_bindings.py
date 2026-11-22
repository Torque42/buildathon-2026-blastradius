"""Freeze the live binding values at the entry of each expensive pinned parser call."""

from collections import Counter
import dis
import gzip
import hashlib
import marshal
import os
from pathlib import Path
import sys
import types
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import binding_state


def codes(c):
    yield c
    for value in c.co_consts:
        if type(value) is types.CodeType:
            yield from codes(value)


def main():
    pin, raw, captured, output = map(lambda a: Path(a).resolve(), sys.argv[1:])
    version, lengths, owners, supers = marshal.loads(gzip.decompress(captured.read_bytes()))
    logical, calls, stages = marshal.loads(gzip.decompress(raw.read_bytes()))
    if tuple(version) != tuple(logical) or tuple(version) != sys.version_info[:2]:
        raise ValueError("Different interpreter versions")
    keys = {}
    compiled = {}
    objects = {}
    for phase in ("2", "3"):
        fields = set(tuple(v) for v in owners[phase])
        for (path, qual, first, digest), offsets in stages[phase].items():
            if path not in compiled:
                source = (pin/path).read_bytes()
                compiled[path] = {(c.co_qualname, c.co_firstlineno, hashlib.blake2s(c.co_code,digest_size=12).digest()): c
                                  for c in codes(compile(source, path, "exec", dont_inherit=True))}
            code = compiled[path][qual, first, digest]
            module = path[:-3].replace("/", ".").removesuffix(".__init__")
            for op in dis.get_instructions(code):
                if (op.opname in {"LOAD_GLOBAL", "LOAD_NAME", "LOAD_FROM_DICT_OR_GLOBALS"} and
                        (offsets & (1 << (op.offset // 2)))):
                    fields.add((module, "", op.argval))
        keys[phase] = tuple(sorted(fields))
    sys.path.insert(0, str(pin))
    os.chdir(pin)
    from tests import test_parser
    from sqlglot.errors import ParseError
    original = test_parser.parse_one
    seen = []

    def observe(sql, *args, **kwargs):
        index = len(seen)
        seen.append(len(sql))
        if len(sql) < 100_000:
            return original(sql,*args,**kwargs)
        phase = str(index)
        expected, size, _, error = calls[index]
        if (hashlib.sha256(sql.encode()).digest(), tuple(sorted(kwargs.items()))) != expected:
            raise ValueError("Unexpected workload")
        values, unknown = binding_state.live(keys[phase])
        objects[phase] = dict(values=values, unknown=unknown, dynamic=tuple(supers.get(phase, ())))
        print(phase, len(values), "resolved", len(unknown), "opaque",
              list(Counter(unknown.values()).items())[:12], file=sys.stderr)
        raise ParseError(error[2])

    test_parser.parse_one = observe
    result = unittest.TestResult()
    unittest.TestSuite([test_parser.TestParser("test_max_nodes")]).run(result)
    if not result.wasSuccessful() or set(objects) != {"2", "3"} or seen != lengths:
        raise ValueError((result.errors, result.failures, seen))
    output.write_bytes(gzip.compress(marshal.dumps((version, objects)), mtime=0))


if __name__ == "__main__":
    main()
