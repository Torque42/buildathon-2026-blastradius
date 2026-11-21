"""Record SQLGlot instructions outside the query calls replayed by the selector."""
import atexit
import functools
import gzip
import hashlib
import importlib.abc
import importlib.machinery
import marshal
import os
import sys
from pathlib import Path

root = Path(os.environ["BR_QUERY_PIN"]).resolve()
prefix = str(root) + "/sqlglot/"
result = Path(os.environ["BR_QUERY_OUT"])
monitor = sys.monitoring
events = monitor.events
slot = 4
depth = 0
buffers = {}
identities = {}
wrapped = False

def enter(code, offset):
    if code.co_filename.startswith(prefix):
        key = (code.co_filename[len(str(root))+1:], code.co_qualname, code.co_firstlineno,
               hashlib.blake2s(code.co_code, digest_size=12).digest())
        identities[id(code)] = (code, key)
        monitor.set_local_events(slot, code, events.INSTRUCTION)
    return monitor.DISABLE

def instruction(code, offset):
    if not depth:
        key = identities[id(code)][1]
        if key not in buffers:
            buffers[key] = bytearray((len(code.co_code)//2 + 7)//8)
        index = offset//2
        buffers[key][index >> 3] |= 1 << (index & 7)
    return monitor.DISABLE

def exclude(fn, caller):
    @functools.wraps(fn)
    def run(*args, **kwargs):
        global depth
        if caller is not None and sys._getframe(1).f_code.co_name != caller:
            return fn(*args, **kwargs)
        depth += 1
        try:
            return fn(*args, **kwargs)
        finally:
            depth -= 1
            if not depth:
                monitor.restart_events()
    return run

class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        global wrapped
        super().exec_module(module)
        module.parse_one = exclude(module.parse_one, "<listcomp>")
        # Python 3.13 inlines comprehensions in the calling function.
        module.parse_one = exclude(module.parse_one, "_mp_execute")
        module.transpile = exclude(module.transpile, "cached_execute")
        module.mp_execute = exclude(module.mp_execute, None)
        wrapped = True

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tests.test_executor":
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
            return spec

def save():
    rows = {key: int.from_bytes(value, "little") | 1 for key, value in buffers.items()}
    (result/(str(os.getpid())+".gz")).write_bytes(gzip.compress(marshal.dumps((wrapped, rows)), mtime=0))

monitor.use_tool_id(slot, "query-harness")
monitor.register_callback(slot, events.PY_START, enter)
monitor.register_callback(slot, events.INSTRUCTION, instruction)
monitor.set_events(slot, events.PY_START)
sys.meta_path.insert(0, Finder())
atexit.register(save)
