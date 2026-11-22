"""Record the actual parser input and exception state at the projections checkpoint."""
import hashlib
import marshal
import os
from pathlib import Path
import sys
import unittest

root=Path(__file__).resolve().parents[1]
pin=root/"cache/pinned"
sys.path[:0]=[str(root),str(pin)]
os.chdir(pin)
import projection_state
import token_streams
from sqlglot.parser import Parser
from sqlglot.errors import ParseError
from tests.test_parser import TestParser

original=Parser._parse_select_query
rows={}
def capture(self,*args,**kwargs):
    if len(self.sql)<100_000:
        return original(self,*args,**kwargs)
    if any(type(x) is not bool for x in (*args,*kwargs.values())):
        raise ValueError("Unrecognized SELECT options")
    key=(token_streams.key(self.sql),self.max_nodes,tuple(args),tuple(sorted(kwargs.items())))
    print("projection enter",self.max_nodes,self._index,file=sys.stderr,flush=True)
    before=projection_state.state(self,True)
    try:
        return original(self,*args,**kwargs)
    except ParseError as exc:
        after=projection_state.state(self,False)
        print("projection exit",self.max_nodes,self._index,file=sys.stderr,flush=True)
        rows[key]=(before,after,str(exc),exc.errors)
        raise
Parser._parse_select_query=capture
result=unittest.TestResult()
unittest.TestSuite([TestParser("test_max_nodes")]).run(result)
if not result.wasSuccessful() or len(rows)!=2:
    raise ValueError((result.failures,result.errors,list(rows)))
(root/"cache/parser-projections").write_bytes(marshal.dumps(rows))
print("Captured",len(rows),"complete projection input and exception states",flush=True)
