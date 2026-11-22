"""Trace actual owners of attributes read by the pristine node-limit regression test."""

import ast
import builtins
from collections import defaultdict
import gzip
import dis
import hashlib
import importlib.abc
import importlib.machinery
import marshal
import os
from pathlib import Path
import sys
import types
import unittest


def main():
    root, out = (Path(v).resolve() for v in sys.argv[1:3])
    sys.path.insert(0, str(root))
    os.chdir(root)
    stage = None
    seen = defaultdict(set)
    classes = {}
    extra = defaultdict(set)

    def observe(obj, name):
        if stage is None or type(name) is not str:
            return obj
        if type(obj) is types.ModuleType:
            module = obj.__name__
            if module.startswith("sqlglot"):
                seen[stage].add((module, "", name))
            return obj
        if type(obj) is super:
            extra[stage].add(name)
            return obj
        cls = obj if isinstance(obj, type) else type(obj)
        slot = id(cls)
        if slot not in classes:
            mod = type.__getattribute__(cls, "__module__")
            scope = (mod, type.__getattribute__(cls, "__qualname__")) if mod.startswith("sqlglot") else None
            classes[slot] = cls, scope
        scope = classes[slot][1]
        if scope is not None:
            seen[stage].add((*scope, name))
        return obj

    def lookup(receiver, name, *fallback):
        observe(receiver, name)
        return getattr(receiver, name, *fallback)

    def has(receiver, name):
        observe(receiver, name)
        return hasattr(receiver, name)

    class Instrument(ast.NodeTransformer):
        def visit_Attribute(self, node):
            self.generic_visit(node)
            if isinstance(node.ctx, ast.Load):
                node.value = ast.copy_location(ast.Call(ast.Name("_br_observe_owner", ast.Load()),
                                                       [node.value, ast.Constant(node.attr)], []), node.value)
            return node

        def visit_Call(self, node):
            self.generic_visit(node)
            if isinstance(node.func, ast.Name) and node.func.id in {"getattr", "hasattr"}:
                node.func.id = "_br_lookup" if node.func.id == "getattr" else "_br_has"
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            tree = ast.parse(Path(self.path).read_bytes(), self.path)
            tree = ast.fix_missing_locations(Instrument().visit(tree))
            return compile(tree, self.path, "exec", dont_inherit=True)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path, target=None):
            if not (fullname == "sqlglot" or fullname.startswith("sqlglot.") or fullname == "tests.test_parser"):
                return
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and spec.origin and spec.origin.endswith(".py"):
                spec.loader = Loader(fullname, spec.origin)
                return spec

    mode=sys.argv[3]
    executed=defaultdict(dict)
    code_ids={}
    mon=sys.monitoring
    slot=4
    if mode=="owners":
        builtins._br_observe_owner, builtins._br_lookup, builtins._br_has = observe, lookup, has
        sys.meta_path.insert(0, Finder())
    elif mode=="code":
        def start(code,offset):
            if stage and code.co_filename.startswith(str(root)+"/sqlglot/"):
                ident=(code.co_filename[len(str(root))+1:],code.co_qualname,code.co_firstlineno,
                       hashlib.blake2s(code.co_code,digest_size=12).digest())
                code_ids[id(code)]=(code,ident)
                mon.set_local_events(slot,code,mon.events.INSTRUCTION)
            return mon.DISABLE
        def ins(code,offset):
            if stage:
                ident=code_ids[id(code)][1]
                executed[stage][ident]=executed[stage].get(ident,0)|(1<<(offset//2))|1
            return mon.DISABLE
        mon.use_tool_id(slot,"original tokenizer and parser checkpoints")
        mon.register_callback(slot,mon.events.PY_START,start)
        mon.register_callback(slot,mon.events.INSTRUCTION,ins)
        mon.set_events(slot,mon.events.PY_START)
    else:
        raise ValueError(mode)
    from tests import test_parser
    from sqlglot.parser import Parser
    from sqlglot.dialects.dialect import Dialect
    orig_lexer,orig_parser=Dialect.tokenize,Parser.parse
    calls=[]
    def in_stage(label,func,args,kwargs):
        nonlocal stage
        last=stage
        stage=label
        calls.append(label)
        if mode=="code":
            mon.restart_events()
        try:
            return func(*args,**kwargs)
        finally:
            stage=last
    def lexer(self,sql,*args,**kwargs):
        if len(sql)>100_000:
            return in_stage("tokenizer",orig_lexer,(self,sql,*args),kwargs)
        return orig_lexer(self,sql,*args,**kwargs)
    def parser(self,tokens,sql,*args,**kwargs):
        if len(sql)>100_000:
            return in_stage("parser",orig_parser,(self,tokens,sql,*args),kwargs)
        return orig_parser(self,tokens,sql,*args,**kwargs)
    Dialect.tokenize=lexer
    Parser.parse=parser
    result = unittest.TestResult()
    unittest.TestSuite([test_parser.TestParser("test_max_nodes")]).run(result)
    stage = None
    if not result.wasSuccessful() or result.testsRun != 1 or result.skipped or calls!=["tokenizer","parser"]*2:
        raise ValueError((result.failures, result.errors, calls))
    if mode=="code":
        mon.set_events(slot,0)
        for code,ident in code_ids.values():
            module=ident[0][:-3].replace("/",".").removesuffix(".__init__")
            for name,rows in executed.items():
                bits=rows.get(ident,0)
                for op in dis.get_instructions(code):
                    if op.opname in {"LOAD_GLOBAL","LOAD_NAME","LOAD_FROM_DICT_OR_GLOBALS"} and bits&(1<<(op.offset//2)):
                        seen[name].add((module,"",op.argval))
    data = (tuple(sys.version_info[:2]), dict(executed),
            {k:sorted(v) for k,v in seen.items()}, {k:sorted(v) for k,v in extra.items()})
    out.write_bytes(gzip.compress(marshal.dumps(data), mtime=0))
    print("pristine node-limit test", calls, {k:len(v) for k,v in seen.items()}, file=sys.stderr)


if __name__ == "__main__":
    main()
