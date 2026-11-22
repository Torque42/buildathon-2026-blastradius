"""Reuse original scan results when input rows and executable row programs are equal."""
import dis
from functools import lru_cache
import hashlib
import marshal
from pathlib import Path
import pickle
from types import CodeType

DIRECTORY=Path(__file__).parent/"cache/row-scans"
stats=[0,0,0]

@lru_cache(2048)
def program(code):
    if code is None:
        return None
    if type(code) is not CodeType or code.co_freevars or code.co_cellvars or code.co_exceptiontable:
        raise ValueError("Nonlocal row calculation")
    if set(code.co_names)-{"scope"} or any(type(c) is CodeType for c in code.co_consts):
        raise ValueError("Row calculation might read external state")
    for op in dis.get_instructions(code):
        if (op.opname.startswith(("STORE_","DELETE_","IMPORT_")) or "ATTR" in op.opname or
                op.opname.startswith("CALL") or op.opname in {"MAKE_FUNCTION","LOAD_BUILD_CLASS"}):
            raise ValueError("Non-scalar row calculation")
    return code.co_code,code.co_consts,code.co_names,code.co_flags


def stamp(engine,context,step,iterator):
    from sqlglot.executor.context import Context
    from sqlglot.executor.python import SUBQUERY_NODES
    from sqlglot.executor.table import Table,TableIter,RowReader
    from sqlglot import exp
    if (type(context) is not Context or type(iterator) is not TableIter or iterator.index!=-1
            or engine._outer_scope or len(context.tables)!=1 or context.env.get("scope") is not context.row_readers):
        return
    name,t=next(iter(context.tables.items()))
    if (type(t) is not Table or iterator.table is not t or type(t.reader) is not RowReader
            or context.row_readers!={name:t.reader} or not 8<=len(t.rows)<=100000):
        return
    exprs=([step.condition] if step.condition is not None else [])+list(step.projections or ())
    if any(not isinstance(e,exp.Expr) or e.find(*SUBQUERY_NODES) for e in exprs):
        return
    try:
        predicate=program(engine.generate(step.condition))
        outputs=tuple(program(c) for c in engine.generate_tuple(step.projections))
    except (ValueError,TypeError):
        return
    columns=tuple(e.alias_or_name for e in step.projections) if step.projections else context.columns
    bounds=None if t.column_range is None else (t.column_range.start,t.column_range.stop,t.column_range.step)
    # The actual RowReader dictionary is observable by every compiled scope lookup.
    header=(name,tuple(t.columns),bounds,tuple(t.reader.columns.items()),columns,step.limit,step.offset,predicate,outputs)
    h=hashlib.sha256(marshal.dumps(header,2))
    h.update(pickle.dumps(t.rows,protocol=5))
    return h.hexdigest(),t


def install(edits,record=False):
    paths=[p for p,_,_ in edits]
    if not record and (not paths or not DIRECTORY.is_dir() or not all(
            p.startswith(("sqlglot/optimizer/","sqlglot/parsers/")) or p in ("sqlglot/parser.py","sqlglot/planner.py")
            for p in paths)):
        return
    from sqlglot.executor.python import PythonExecutor
    from sqlglot.executor.table import Table
    original=PythonExecutor._project_and_filter
    def cached(engine,context,step,iterator):
        key=stamp(engine,context,step,iterator)
        if key is None:
            return original(engine,context,step,iterator)
        digest,table=key
        path=DIRECTORY/digest
        if not record and path.is_file():
            columns,rows,position=pickle.loads(path.read_bytes())
            if not (0<=position<=len(table.rows)):
                return original(engine,context,step,iterator)
            iterator.index=position
            table.reader.row=table.rows[min(position,len(table.rows)-1)]
            stats[1]+=1
            return Table(columns,rows)
        result=original(engine,context,step,iterator)
        if record and type(result) is Table and not result.column_range and 0<=iterator.index<=len(table.rows):
            saved=pickle.dumps((result.columns,result.rows,iterator.index),protocol=5)
            if len(saved)<=400000:
                if path.is_file() and path.read_bytes()!=saved:
                    raise ValueError("An identical concrete scan produced different output")
                path.write_bytes(saved)
                stats[0]+=1
        else:
            stats[2]+=1
        return result
    PythonExecutor._project_and_filter=cached
    return original
