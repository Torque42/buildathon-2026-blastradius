"""Observe the two actual long calls and the original assertions on the pristine checkout."""
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
import binding_state
import shortlist
import token_streams
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import ParseError
from sqlglot.parser import Parser
from tests.test_parser import TestParser

original_lexer,original_parser=Dialect.tokenize,Parser.parse
errors={}
streams={}
def tokenize(self,sql,*args,**kwargs):
    actual=original_lexer(self,sql,*args,**kwargs)
    if len(sql)>100_000:
        if args or kwargs:
            raise ValueError("Unexpected tokenizer arguments")
        k=token_streams.key(sql)
        recorded=token_streams.encode(actual)
        if recorded is None or k in streams and streams[k]!=recorded:
            raise ValueError("Unstable tokenizer result")
        streams[k]=recorded
    return actual
def parse(self,tokens,sql,*args,**kwargs):
    try:
        return original_parser(self,tokens,sql,*args,**kwargs)
    except ParseError as exc:
        if len(sql)>100_000:
            if type(exc) is not ParseError or args or kwargs:
                raise ValueError("Unrecognized parser exit")
            errors[(token_streams.key(sql),self.max_nodes)]=str(exc)
        raise
Dialect.tokenize,Parser.parse=tokenize,parse
state=shortlist.witness()
state[:]=[hashlib.blake2s(digest_size=16),0]
result=unittest.TestResult()
unittest.TestSuite([TestParser("test_max_nodes")]).run(result)
if not result.wasSuccessful() or result.testsRun!=1 or result.skipped or len(streams)!=1 or len(errors)!=2:
    raise RuntimeError((result.errors,result.failures,len(streams),len(errors)))
data=dict(errors=errors,streams=streams,assertions=(state[1],state[0].digest()),
          bridge=binding_state.code_hash(Dialect.parse.__code__))
(root/"cache/parser-frontiers").write_bytes(marshal.dumps(data))
print("Captured",len(errors),"real parser exits,",len(streams),"complete lexical stream and",state[1],"assertions")
