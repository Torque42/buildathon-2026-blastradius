"""Collect audited default-value and code-object introspection on pinned functions."""
import atexit
import gzip
import json
import os
import sys
import types
from pathlib import Path
prefix=os.environ["BR_ROOT"]+"/sqlglot/"
active=""
seen=set();inside=False
def audit(event,args):
    global inside
    if inside or event!="object.__getattr__" or args[1] not in ("__defaults__","__kwdefaults__","__code__"):return
    if not isinstance(args[0],types.FunctionType):return
    inside=True
    try:
        code=args[0].__code__
        if code.co_filename.startswith(prefix):
            seen.add("sqlglot/"+code.co_filename[len(prefix):]+":"+str(code.co_firstlineno))
    finally:inside=False
def save():
    (Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".json.gz")).write_bytes(gzip.compress(json.dumps([sorted(seen),{}]).encode(),mtime=0))
sys.addaudithook(audit);atexit.register(save)
