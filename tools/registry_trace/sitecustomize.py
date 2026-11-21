"""Record the global names consumed by SQLGlot's compiled executor expressions."""
import atexit
import gzip
import json
import os
import sys
from pathlib import Path
from types import CodeType

active = os.environ.get("BR_FLAG_TEST", "")
reads = {}
seen = set()

def audit(event, args):
    if event != "exec":
        return
    try:
        caller = sys._getframe(1).f_code
    except ValueError:
        return
    if not caller.co_filename.endswith("/sqlglot/executor/context.py") or caller.co_name != "eval":
        return
    code = args[0]
    if not isinstance(code, CodeType):
        return
    key = (code, active)
    if key in seen:
        return
    seen.add(key)
    todo = [code]
    while todo:
        current = todo.pop()
        for symbol in current.co_names:
            reads.setdefault(symbol, set()).add(active)
        todo.extend(value for value in current.co_consts if isinstance(value, CodeType))

def save():
    dest = Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid()) + ".json.gz")
    dest.write_bytes(gzip.compress(json.dumps({key: sorted(ids) for key,ids in reads.items()}).encode(), mtime=0))
sys.addaudithook(audit)
atexit.register(save)
