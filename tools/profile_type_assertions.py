"""Index original cold pytest calls by the individual SQL type assertion they served."""
from contextlib import contextmanager
from functools import lru_cache
import gzip
import hashlib
import json
import marshal
import os
from pathlib import Path
import sys
import unittest

root=Path(sys.argv[1]).resolve()
dest=Path(sys.argv[2])
sys.path.insert(0,str(root))
os.chdir(root)
import pytest
original=unittest.TestCase.subTest
active=0
cases=[]
schema=None
seen={}
version=tuple(sys.version_info[:2])
mon=sys.monitoring
tool=4

@contextmanager
def subtest(self,*args,**kwargs):
    global active,schema
    f=sys._getframe(2)
    target=f.f_code.co_name=="test_annotate_funcs" and f.f_code.co_filename.endswith("/tests/test_optimizer.py")
    with original(self,*args,**kwargs):
        if not target:
            yield
            return
        values=f.f_locals
        sample=(values["sql"],values["expected"],values["dialect"])
        if schema is None:
            schema=json.loads(json.dumps(values["test_schema"]))
        if schema!=values["test_schema"]:raise ValueError("Schema changed across cases")
        cases.append(sample)
        active=len(cases)
        mon.restart_events()
        try:
            yield
        finally:
            active=0
            mon.restart_events()

@lru_cache(None)
def identity(code):
    return (code.co_filename[len(str(root))+1:],code.co_qualname,code.co_firstlineno,
            hashlib.blake2s(code.co_code,digest_size=12).digest())

def start(code,offset):
    if code.co_filename.startswith(str(root)+"/sqlglot/"):
        key=identity(code)
        seen[key]=seen.get(key,0)|(1<<active)
    return mon.DISABLE

unittest.TestCase.subTest=subtest
mon.use_tool_id(tool,"SQL type assertion dependencies")
mon.register_callback(tool,mon.events.PY_START,start)
mon.set_events(tool,mon.events.PY_START)
status=pytest.main(["-q","--tb=short","-p","no:cacheprovider","tests/test_optimizer.py::TestOptimizer::test_annotate_funcs"])
mon.set_events(tool,0)
if status or len(cases)<500 or len(seen)<150:
    raise ValueError((status,len(cases),len(seen)))
masks=list(set(seen.values()))
index={bits:i for i,bits in enumerate(masks)}
dest.write_bytes(gzip.compress(marshal.dumps((version,cases,schema,masks,{k:index[v] for k,v in seen.items()})),mtime=0))
print(len(cases),len(seen),len(masks),dest.stat().st_size,flush=True)
