"""Run supported SQLGlot row projections directly on the actual table tuples."""

import ast
from functools import lru_cache
import math
from operator import itemgetter
from types import CodeType, FunctionType


def identical(source, code):
    baseline=compile(source,code.co_filename,"eval",dont_inherit=True,optimize=2)
    return (baseline.co_code==code.co_code and baseline.co_names==code.co_names and
            baseline.co_consts==code.co_consts and not code.co_cellvars and not code.co_freevars and
            not code.co_exceptiontable)


@lru_cache(2048)
def project(code):
    if type(code) is not CodeType or len(code.co_filename)>2600:
        return
    try:
        if not identical(code.co_filename,code):
            return
        body=ast.parse(code.co_filename,mode="eval").body
    except (SyntaxError,ValueError,OverflowError):
        return
    return body


def column(v):
    if (isinstance(v,ast.Subscript) and isinstance(v.value,ast.Subscript) and
            isinstance(v.value.value,ast.Name) and v.value.value.id=="scope" and
            isinstance(v.value.slice,ast.Constant) and type(v.value.slice.value) in (type(None),str) and
            isinstance(v.slice,ast.Constant) and type(v.slice.value) is str):
        return v.value.slice.value,v.slice.value


def getter(indexes):
    if len(indexes)==1:
        i=indexes[0]
        return lambda row: (row[i],)
    return itemgetter(*indexes) if indexes else lambda row: ()


def direct_tuple(codes,scope):
    from sqlglot.executor.table import RowReader
    if type(codes) is not tuple or type(scope) is not dict:
        return
    source=None
    indexes=[]
    for code in codes:
        node=project(code)
        ref=column(node)
        if ref is None:
            return
        alias,name=ref
        reader=scope.get(alias)
        if type(reader) is not RowReader or type(reader.columns) is not dict:
            return
        if source is not None and reader is not source:
            return
        source=reader
        index=reader.columns.get(name)
        if type(index) is not int:
            return
        indexes.append(index)
    if source is not None:
        return source,getter(indexes)


class Columns(ast.NodeTransformer):
    def __init__(self,scope,source):
        self.scope=scope
        self.source=source

    def visit_Subscript(self,node):
        ref=column(node)
        if ref is None or self.scope.get(ref[0]) is not self.source:
            raise ValueError
        index=self.source.columns.get(ref[1])
        if type(index) is not int:
            raise ValueError
        return ast.Subscript(ast.Name("_r",ast.Load()),ast.Constant(index),ast.Load())

    def visit_Call(self,node):
        if not isinstance(node.func,ast.Name) or not node.func.id.isupper() or node.keywords:
            raise ValueError
        if node.func.id.startswith("SUBQUERY") or node.func.id not in PURE_NAMES:
            raise ValueError
        return ast.Call(node.func,[self.visit(c) for c in node.args],[])

    def visit_Name(self,node):
        raise ValueError

    def generic_visit(self,node):
        if not isinstance(node,(ast.Constant,ast.Tuple,ast.List,ast.Load,ast.IfExp,ast.UnaryOp,ast.BoolOp,
                                ast.BinOp,ast.Compare,ast.And,ast.Or,ast.Not,ast.USub,ast.UAdd,
                                ast.Add,ast.Sub,ast.Mult,ast.Div,ast.Mod,ast.Eq,ast.NotEq,ast.Is,
                                ast.IsNot,ast.In,ast.NotIn,ast.Lt,ast.LtE,ast.Gt,ast.GtE)):
            raise ValueError
        # NodeTransformer mutates its input. Reconstruct nodes to keep cached source trees pristine.
        clone=type(node)()
        for name,value in ast.iter_fields(node):
            if isinstance(value,list):
                setattr(clone,name,[self.visit(v) if isinstance(v,ast.AST) else v for v in value])
            else:
                setattr(clone,name,self.visit(value) if isinstance(value,ast.AST) else value)
        return clone


PURE_NAMES=frozenset("ADD SUB MUL DIV MOD NEG BITWISEAND BITWISEOR EQ NEQ GT GTE LT LTE AND OR NOT IS IN BETWEEN "
                     "COALESCE CONCAT LOWER UPPER LENGTH SUBSTRING LIKE ILIKE ABS ROUND POW NULLIF".split())


def compile_loop(context,reader,codes,condition):
    transform=Columns(context.row_readers,reader)
    def translate(code):
        tree=project(code)
        if tree is None:
            raise ValueError
        return transform.visit(tree)
    try:
        outputs=ast.Tuple([translate(c) for c in codes],ast.Load()) if codes else ast.Name("_r",ast.Load())
        guards=[translate(condition)] if condition is not None else []
        comp=ast.ListComp(outputs,[ast.comprehension(ast.Name("_r",ast.Store()),ast.Name("_rows",ast.Load()),guards,0)])
        args=ast.arguments(posonlyargs=[],args=[ast.arg(arg="_rows")],kwonlyargs=[],kw_defaults=[],defaults=[])
        template=ast.Expression(ast.Lambda(args,comp))
        compiled=compile(ast.fix_missing_locations(template),"<SQLGlot rows>","eval",dont_inherit=True,optimize=2)
        fn=next(c for c in compiled.co_consts if type(c) is CodeType)
        # The originals use eval with exactly this globals dictionary. Source
        # column reads are equivalent by the checked RowReader mapping.
        return FunctionType(fn,context.env)
    except (KeyError,ValueError,StopIteration,SyntaxError,TypeError):
        return


def install(edits):
    if not edits or not all(p.startswith("sqlglot/optimizer/") or p in ("sqlglot/parser.py","sqlglot/planner.py")
                            or p.startswith("sqlglot/parsers/") for p,_,_ in edits):
        return
    from sqlglot import exp
    from sqlglot.executor.context import Context
    from sqlglot.executor.python import PythonExecutor,SUBQUERY_NODES
    from sqlglot.executor.table import Table,TableIter,RowReader
    old_tuple=Context.eval_tuple
    def compute(ctx,codes):
        scope=ctx.env.get("scope")
        if scope is not ctx.row_readers:
            return old_tuple(ctx,codes)
        saved=getattr(ctx,"_br_direct_tuple",None)
        if saved is None or saved[0] is not codes or saved[1] is not scope:
            saved=(codes,scope,direct_tuple(codes,scope))
            ctx._br_direct_tuple=saved
        fast=saved[2]
        if fast is not None and type(fast[0].row) is tuple:
            return fast[1](fast[0].row)
        return old_tuple(ctx,codes)
    Context.eval_tuple=compute
    original=PythonExecutor._project_and_filter
    def rows(engine,context,step,iterator):
        if (type(iterator) is not TableIter or iterator.index!=-1 or type(iterator.table) is not Table or
                type(iterator.table.rows) is not list or len(iterator.table.rows)<64 or
                type(iterator.table.reader) is not RowReader or engine._outer_scope is not None or
                context.env.get("scope") is not context.row_readers or type(context.row_readers) is not dict or
                not all(type(row) is tuple for row in iterator.table.rows[:1]) or
                any(not isinstance(e,exp.Expr) or e.find(*SUBQUERY_NODES) for e in
                    (*step.projections,*([step.condition] if step.condition is not None else [])))):
            return original(engine,context,step,iterator)
        # A finite limit with a filter can stop the original before evaluating a
        # later row. Use its exact loop when early termination is data dependent.
        limit=step.offset+step.limit
        if (type(limit) not in (float,int) or limit<0 or
                (not math.isinf(limit) and (step.condition is not None or int(limit)!=limit))):
            return original(engine,context,step,iterator)
        sink=engine.table(step.projections if step.projections else context.columns)
        condition=engine.generate(step.condition)
        codes=engine.generate_tuple(step.projections)
        source=iterator.table
        if codes and condition is None:
            projection=direct_tuple(codes,context.row_readers)
        else:
            projection=None
        fn=None
        if not (projection and projection[0] is source.reader):
            fn=compile_loop(context,source.reader,codes,condition)
            if fn is None:
                for reader in iterator:
                    if len(sink)>=limit:
                        break
                    if condition and not context.eval(condition):
                        continue
                    sink.append(context.eval_tuple(codes) if codes else reader.row)
                return sink
        data=source.rows if math.isinf(limit) or limit>=len(source.rows) else source.rows[:int(limit)]
        sink.rows=list(map(projection[1],data)) if fn is None else fn(data)
        if sink.rows and len(sink.rows[0])!=len(sink.columns):
            raise ValueError("Wrong projection width")
        i=len(data)
        iterator.index=i
        if source.rows:
            source.reader.row=source.rows[min(i,len(source.rows)-1)]
        return sink
    PythonExecutor._project_and_filter=rows
