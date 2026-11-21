"""Locate literal edits to tokenizer dictionaries and writes to ordinary slot members."""
import ast
import re

WORD = re.compile(r"[A-Z][A-Z0-9_]*\Z")

def literal(value):
    return isinstance(value, ast.Constant) and type(value.value) in (bool, int, str, type(None))

def scan(path, tree):
    rows = []
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
        slots = set()
        for stmt in cls.body:
            if not isinstance(stmt, (ast.Assign,ast.AnnAssign)):
                continue
            target = stmt.targets[0] if isinstance(stmt,ast.Assign) and len(stmt.targets)==1 else getattr(stmt,"target",None)
            if not isinstance(target,ast.Name):
                continue
            if target.id=="__slots__" and isinstance(stmt.value,(ast.Tuple,ast.List)):
                slots.update(v.value for v in stmt.value.elts if literal(v) and type(v.value) is str)
            if target.id!="KEYWORDS" or not isinstance(stmt.value,ast.Dict):
                continue
            if path!="sqlglot/tokens.py" and not (path.startswith("sqlglot/dialects/") or path=="sqlglot/jsonpath.py"):
                continue
            for key,token in zip(stmt.value.keys,stmt.value.values):
                if not literal(key) or type(key.value) is not str or not WORD.fullmatch(key.value):
                    continue
                if not isinstance(token,ast.Attribute) or not isinstance(token.value,ast.Name) or token.value.id!="TokenType":
                    continue
                for node,kind in ((key,"key"),(token,"token")):
                    if node.lineno==node.end_lineno:
                        rows.append((node.lineno,node.col_offset,node.end_col_offset,kind,key.value,token.attr,0))
        if not slots:
            continue
        for fn in cls.body:
            if not isinstance(fn,ast.FunctionDef) or fn.decorator_list or not fn.args.args or fn.args.args[0].arg!="self":
                continue
            if any(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id in ("eval","exec","locals","vars","super") and (n.func.id!="super" or len(n.args)>0) for n in ast.walk(fn)):
                continue
            params=[*fn.args.posonlyargs,*fn.args.args]
            defaults=dict(zip([v.arg for v in params[-len(fn.args.defaults):]],fn.args.defaults))
            defaults.update((v.arg,a) for v,a in zip(fn.args.kwonlyargs,fn.args.kw_defaults) if a is not None)
            for stmt in ast.walk(fn):
                if isinstance(stmt,ast.Assign) and len(stmt.targets)==1: target=stmt.targets[0]
                elif isinstance(stmt,ast.AnnAssign):target=stmt.target
                else:continue
                if not isinstance(target,ast.Attribute) or not isinstance(target.value,ast.Name) or target.value.id!="self" or target.attr not in slots:
                    continue
                val=stmt.value
                kind="slot"
                if isinstance(val,ast.Name) and val.id in defaults:
                    if sum(isinstance(n,ast.Name) and n.id==val.id for n in ast.walk(fn))!=1:continue
                    val=defaults[val.id];kind="default"
                if literal(val) and val.lineno==val.end_lineno:
                    key=path+":"+cls.name+":"+target.attr
                    rows.append((val.lineno,val.col_offset,val.end_col_offset,kind,key,type(val.value).__name__,fn.lineno))
    return rows
