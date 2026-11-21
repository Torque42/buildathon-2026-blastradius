"""Collect executed offsets on the pristine pin in a single cold pytest invocation."""
import atexit
import hashlib
import marshal
import os
import sys
from pathlib import Path

prefix = os.environ["BR_PIN"] + "/sqlglot/"
destination = Path(os.environ["BR_OFFSETS_OUT"])
tool = 4
events = sys.monitoring.events
by_code = {}
records = {}

def start(code, offset):
    filename = code.co_filename
    if filename.startswith(prefix):
        path = "sqlglot/" + filename[len(prefix):]
        member = (code.co_qualname, code.co_firstlineno, hashlib.blake2s(code.co_code, digest_size=12).digest())
        key = (path, member)
        bucket = records.get(key)
        if bucket is None:
            bucket = records[key] = bytearray((len(code.co_code)//2 + 7)//8)
        by_code[id(code)] = (code, bucket)
        sys.monitoring.set_local_events(tool, code, events.INSTRUCTION)
    return sys.monitoring.DISABLE

def instruction(code, offset):
    index = offset // 2
    by_code[id(code)][1][index >> 3] |= 1 << (index & 7)
    return sys.monitoring.DISABLE

def flush():
    payload = {key: bytes(mask) for key, mask in records.items()}
    (destination / (str(os.getpid()) + ".rec")).write_bytes(marshal.dumps(payload))

atexit.register(flush)
sys.monitoring.use_tool_id(tool, "original-offsets")
sys.monitoring.register_callback(tool, events.PY_START, start)
sys.monitoring.register_callback(tool, events.INSTRUCTION, instruction)
sys.monitoring.set_events(tool, events.PY_START)
