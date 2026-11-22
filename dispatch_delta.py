"""Find the readers of changed parser and generator dispatch entries, including inherited copies."""
import ast
from functools import lru_cache
import lzma
import marshal
from pathlib import Path

import workload_replay


@lru_cache(1)
def facts():
    p=Path(__file__).parent/"maps/dialect-table-reads.xz"
    names,masks,tables=marshal.loads(lzma.decompress(p.read_bytes()))
    return {n:1<<i for i,n in enumerate(names)},masks,tables


@lru_cache(1)
def symbols():
    path=Path(__file__).parent/"maps/dialect-symbols.xz"
    return marshal.loads(lzma.decompress(path.read_bytes()))


def stable_key(node):
    if isinstance(node,ast.Constant) and type(node.value) is str:
        return "string:"+node.value
    if isinstance(node,ast.Attribute):
        if isinstance(node.value,ast.Name) and node.value.id=="exp":
            return symbols()[0].get(node.attr)
        if isinstance(node.value,ast.Name) and node.value.id in ("TokenType","DType"):
            return symbols()[1 if node.value.id=="TokenType" else 2].get(node.attr)
        if ast.unparse(node.value) in ("exp.DType", "exp.DataType.Type"):
            return symbols()[2].get(node.attr)


def pure(node):
    if isinstance(node,ast.Constant):
        return type(node.value) in (str,int,float,bool,type(None))
    if isinstance(node,ast.Lambda):
        return not node.args.defaults and not any(node.args.kw_defaults)
    if isinstance(node,ast.Attribute):
        return stable_key(node) is not None
    if isinstance(node,(ast.Tuple,ast.List)):
        return all(pure(n) for n in node.elts)
    return False


def table_declarations(tree,path,available):
    module=path[:-3].replace("/",".")
    out={}
    def visit(stmts,classes):
        for stmt in stmts:
            if isinstance(stmt,ast.ClassDef):
                visit(stmt.body,(*classes,stmt.name))
                continue
            target=(stmt.target if isinstance(stmt,ast.AnnAssign) else stmt.targets[0]
                    if isinstance(stmt,ast.Assign) and len(stmt.targets)==1 else None)
            if not classes or not isinstance(target,ast.Name) or not isinstance(stmt.value,ast.Dict):
                continue
            key=module+":"+".".join(classes)+":"+target.id
            if key in available:
                out[key]=stmt
    visit(tree.body,())
    return out


def entries(node):
    recognized={}
    rest=[]
    anchors=[]
    for k,v in zip(node.keys,node.values,strict=True):
        key=stable_key(k)
        if key is None:
            rest.append((ast.dump(k) if k is not None else None,ast.dump(v)))
            continue
        # A key that moves across a spread can bind a different callback.
        if key in recognized:
            return
        recognized[key]=ast.dump(v),len(rest)
        if not pure(v):
            anchors.append((key,ast.dump(v)))
    return recognized,rest,anchors


def changed(a,b):
    left,right=entries(a),entries(b)
    if left is None or right is None or left[1:]!=right[1:]:
        return
    diff={k for k in left[0].keys()|right[0].keys() if left[0].get(k)!=right[0].get(k)}
    if not diff:
        return
    return diff


def describe(selector,path,patch,available):
    before=selector.SRC[path]
    after,removed,added=selector.apply(before,patch["hunks"])
    if not removed and not added:
        return {}
    if path in selector.profile()["source_reads"]:
        return
    lines=before.splitlines()
    for hunk in patch["hunks"]:
        context=[line[1:].rstrip("\r") for line in hunk["lines"] if line.startswith((" ","-"))]
        offset=max(0,hunk["os"]-1)
        if lines[offset:offset+len(context)]!=context:
            return
    original,modified=ast.parse(before),ast.parse(after)
    lhs,rhs=table_declarations(original,path,available),table_declarations(modified,path,available)
    if lhs.keys()!=rhs.keys():
        return
    changes={}
    for name in lhs:
        a,b=lhs[name],rhs[name]
        if ast.dump(a.value)==ast.dump(b.value):
            continue
        diff=changed(a.value,b.value)
        if not diff:
            return
        changes[name]=diff
        a.value=ast.Constant(None)
        b.value=ast.Constant(None)
    if changes and ast.dump(original)==ast.dump(modified):
        return changes


def refine(selector,text,selected):
    if not selected or not any(s in text for s in ("sqlglot/parser","sqlglot/generator")):
        return selected
    patched=selector.parse_patch(text)
    if not 1<=len(patched)<=6:
        return selected
    touched=set()
    for part in patched:
        p=part["old"]
        if not p or p in touched or p!=part["new"] or p not in selector.SRC or not (
                p in ("sqlglot/parser.py","sqlglot/generator.py") or p.startswith(("sqlglot/parsers/","sqlglot/generators/"))):
            return selected
        touched.add(p)
    names,masks,tables=facts()
    if set(names)!=set(selector.NUM):
        return selected
    impact=0
    found=False
    for form in patched:
        changes=describe(selector,form["old"],form,tables)
        if changes is None:
            return selected
        for site,keys in changes.items():
            seen=set()
            todo=[site]
            while todo:
                name=todo.pop()
                if name in seen:
                    continue
                seen.add(name)
                rows=tables.get(name)
                if rows is None:
                    return selected
                todo.extend(rows.get("@copy",()))
                for key in keys|{"*"}:
                    if key in rows:
                        impact|=masks[rows[key]]
            found=True
    if not found:
        return selected
    chosen={n for n in selected if names[selector.TEST_FILE_ID[n]] & impact}
    protect=workload_replay.file_readers(selector,touched)
    return selected & (chosen|protect|selector.UNTRACED_POOL_TESTS)
