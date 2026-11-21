"""Observe which fields of class-scoped query fixtures each pristine test consumes."""
import ast
import dis
import gzip
import inspect
import json
import os
import sys
import unittest
from pathlib import Path

tools=Path(__file__).resolve().parent
pin=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(pin))
os.environ.update(SKIP_INTEGRATION="0",PYTHON_CPU_COUNT="2")
from tests import test_executor as suite

src=Path(suite.__file__).read_text()
tree=ast.parse(src)
rules={}
for fn in (n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef)):
    if any(isinstance(n,ast.Name) and n.id=="_" and isinstance(n.ctx,ast.Load) for n in ast.walk(fn)):continue
    for n in ast.walk(fn):
        if isinstance(n,(ast.For,ast.comprehension)):targets=[n.target]
        elif isinstance(n,ast.Assign):targets=n.targets
        else:continue
        for target in targets:
            for left in ast.walk(target):
                if isinstance(left,(ast.Tuple,ast.List)) and len(left.elts)==3 and all(isinstance(v,ast.Name) for v in left.elts):
                    rows=rules.setdefault((fn.name,left.lineno),set())
                    rows.add(sum(1<<i for i,v in enumerate(left.elts) if v.id!="_"))
context=""
touched={}
paths={}
def mark(file,i,mask):
    if context:touched.setdefault(context,{}).setdefault(file,{}).setdefault(i,set()).add(mask)
class Row(tuple):
    def __new__(cls,values,file,i):
        row=super().__new__(cls,values);row.file=file;row.i=i;return row
    def __iter__(self):
        frame=sys._getframe(1)
        choices=rules.get((frame.f_code.co_name,frame.f_lineno))
        mask=7
        if frame.f_code.co_filename==suite.__file__ and dis.opname[frame.f_code.co_code[frame.f_lasti]]=="UNPACK_SEQUENCE" and choices and len(choices)==1:
            mask=next(iter(choices))
        mark(self.file,self.i,mask)
        return super().__iter__()
    def __getitem__(self,k):
        mark(self.file,self.i,(1<<k) if type(k) is int and 0<=k<3 else 7)
        return super().__getitem__(k)
    def __repr__(self):mark(self.file,self.i,7);return super().__repr__()
    def __hash__(self):mark(self.file,self.i,7);return super().__hash__()
    def __eq__(self,other):mark(self.file,self.i,7);return super().__eq__(other)
    def __ne__(self,other):mark(self.file,self.i,7);return super().__ne__(other)
    def __reduce_ex__(self,protocol):mark(self.file,self.i,7);return tuple,(tuple(self),)
original=suite.load_sql_fixture_pairs
def traced(file):
    path="tests/fixtures/"+file
    paths[path]=(pin/path).read_text()
    for i,row in enumerate(original(file)):
        yield Row(row,path,i)
suite.load_sql_fixture_pairs=traced
class Result(unittest.TestResult):
    def startTest(self,test):
        global context
        context="tests/test_executor.py::TestExecutor::"+test._testMethodName
        touched[context]={}
        super().startTest(test)
    def stopTest(self,test):
        global context
        context=""
        super().stopTest(test)
if __name__=="__main__":
    suite_instance=unittest.defaultTestLoader.loadTestsFromTestCase(suite.TestExecutor)
    result=Result();suite_instance.run(result)
    if not result.wasSuccessful() or result.skipped:raise ValueError(len(result.failures),[(str(t),err[-450:]) for t,err in result.errors])
    reads={}
    for node,by_path in touched.items():
        reads[node]={}
        for path,rows in by_path.items():
            masks={}
            for i,parts in rows.items():
                value=0
                for part in parts:value|=part
                masks[i]=value
            reads[node][path]=masks
    out={"paths":paths,"reads":reads}
    (tools.parent/"maps/fixture-fields.json.gz").write_bytes(gzip.compress(json.dumps(out,separators=(",",":")).encode(),mtime=0))
    print(len(paths),len(reads),[(n,{p:sorted(set(m.values())) for p,m in by_path.items()}) for n,by_path in reads.items() if by_path])
