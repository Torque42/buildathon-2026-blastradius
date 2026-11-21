"""Resolve class equality from its recorded operand types and the actual method control flow."""
import ast
from functools import lru_cache
import gzip
import marshal
from pathlib import Path

import identity_select
from replay_edit import preserve_scope

UNKNOWN = object()
FALLTHROUGH = object()
DEFER = object()

@lru_cache(1)
def profile():
    names,sites=marshal.loads(gzip.decompress((Path(__file__).parent/"maps/class-comparisons.gz").read_bytes()))
    return set(names),sites

@lru_cache(64)
def method(selector,path,line):
    if path not in selector.SRC:
        return None
    for node in ast.walk(selector.source_tree(path)):
        if isinstance(node,ast.FunctionDef) and node.lineno==line and node.name=="__eq__" and not node.decorator_list:
            if len(node.args.args)==2 and not node.args.vararg and not node.args.kwarg:
                return node
    return None


def dispatch(selector, desc, equal, same_type, edited):
    if desc[0]=="identity":
        return True if equal else DEFER
    _,path,line,checks=desc
    code=method(selector,path,line)
    if code is None:return UNKNOWN
    left,right=[a.arg for a in code.args.args]
    checks={target:(a,b) for target,a,b in checks}
    def value(expr):
        if (path,expr.lineno,expr.col_offset)==edited:
            # Re-entering the replaced comparison changes the execution itself.
            return UNKNOWN
        if isinstance(expr,ast.Constant) and type(expr.value) is bool:
            return expr.value
        if isinstance(expr,ast.Name) and expr.id=="NotImplemented":
            return DEFER
        if (isinstance(expr,ast.Call) and isinstance(expr.func,ast.Name) and expr.func.id=="isinstance"
                and len(expr.args)==2 and not expr.keywords and all(isinstance(a,ast.Name) for a in expr.args)):
            var,target=[a.id for a in expr.args]
            if var not in (left,right) or target not in checks:return UNKNOWN
            return checks[target][var==right]
        if isinstance(expr,ast.UnaryOp) and isinstance(expr.op,ast.Not):
            val=value(expr.operand)
            return not val if type(val) is bool else UNKNOWN
        if isinstance(expr,ast.BoolOp):
            for child in expr.values:
                val=value(child)
                if type(val) is not bool:return UNKNOWN
                if isinstance(expr.op,ast.And) and not val:return False
                if isinstance(expr.op,ast.Or) and val:return True
            return val
        if (isinstance(expr,ast.Compare) and len(expr.ops)==1 and isinstance(expr.ops[0],(ast.Is,ast.IsNot))):
            a,b=expr.left,expr.comparators[0]
            if isinstance(a,ast.Name) and isinstance(b,ast.Name) and {a.id,b.id}=={left,right}:
                answer=equal
            elif all(isinstance(v,ast.Call) and isinstance(v.func,ast.Name) and v.func.id=="type" and
                     len(v.args)==1 and not v.keywords and isinstance(v.args[0],ast.Name) for v in (a,b)) and {a.args[0].id,b.args[0].id}=={left,right}:
                answer=same_type
            else:return UNKNOWN
            return answer != isinstance(expr.ops[0],ast.IsNot)
        return UNKNOWN
    def block(stmts):
        for st in stmts:
            if isinstance(st,ast.Expr) and isinstance(st.value,ast.Constant) and isinstance(st.value.value,str):
                continue
            if isinstance(st,ast.If):
                outcome=value(st.test)
                if type(outcome) is not bool:return UNKNOWN
                answer=block(st.body if outcome else st.orelse)
                if answer is not FALLTHROUGH:return answer
            elif isinstance(st,ast.Return) and st.value is not None:
                return value(st.value)
            else:return UNKNOWN
        return FALLTHROUGH
    return block(code.body)


def preserved(selector, state, site):
    if state is None:return False
    equal,reverse,a,b,same=state
    for desc in (b,a) if reverse else (a,b):
        outcome=dispatch(selector,desc,equal,same,site)
        if outcome is DEFER:continue
        return type(outcome) is bool and outcome==equal
    return True


def match(selector,text):
    if " is " not in text or "==" not in text:
        return
    patches=selector.parse_patch(text)
    if len(patches)!=1:return
    patch=patches[0];path=patch["old"]
    if not path or path!=patch["new"] or path not in selector.SRC or path in selector.profile()["source_reads"]:return
    original=selector.SRC[path]
    changed,removed,added=selector.apply(original,patch["hunks"])
    if len(removed)!=1 or len(added)!=1:return
    line=removed[0];a,b=original.splitlines(),changed.splitlines()
    if not a[line-1].isascii() or a[:line-1]!=b[:line-1] or a[line:]!=b[line:]:return
    catalog,sites=profile()
    if catalog!=set(selector.NUM):return
    for start,end,op,key,_ in identity_select.locations(selector,path).get(line,()):
        if op!="==" or key not in sites:continue
        left,right=a[line-1],b[line-1];suffix=left[end:]
        if not right.startswith(left[:start]) or not right.endswith(suffix) or right[start:len(right)-len(suffix)].strip()!="==":
            continue
        position=(path,line,int(key.rsplit(":",1)[1]))
        keep=selector.tests_of(selector.M["opens"].get(path)) | selector.UNTRACED_POOL_TESTS
        keep.update(n for n,(opened,_) in selector.EXTRA.items() if path in opened)
        keep.update(preserve_scope(selector,path,1<<line,selector.ALL))
        for state,contexts in sites[key]:
            if preserved(selector,state,position):continue
            for ctx in contexts:
                if ctx in selector.NUM:keep.add(selector.NUM[ctx])
                elif ctx.startswith("scope:") and ctx[6:] in selector.BY_FILE:keep.update(selector.BY_FILE[ctx[6:]])
                else:return
        return keep
