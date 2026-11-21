"""Compare the changed lookup against pristine container shapes and key presence."""
import ast
import gzip
import marshal
from functools import lru_cache
from pathlib import Path

from access_sites import entries,number
from replay_edit import preserve_scope

ROOT=Path(__file__).resolve().parent

@lru_cache(1)
def facts():
    names,rows=marshal.loads(gzip.decompress((ROOT/"maps/access-shapes.gz").read_bytes()))
    return {n:1<<i for i,n in enumerate(names)},rows
@lru_cache(1)
def index():
    path=ROOT/"cache/access-sites"
    return marshal.loads(path.read_bytes()) if path.exists() else {}
@lru_cache(24)
def sites(selector,path):
    idx=index()
    return idx.get(path,{}) if idx else entries(path,selector.source_tree(path))

def result(shape,idx,kind):
    if -len(shape)<=idx<len(shape):return shape[idx]
    return 0 if kind=="seq" else None

def refine(selector,text,selected):
    if not selected:return selected
    patches=selector.parse_patch(text)
    if len(patches)!=1:return selected
    patch=patches[0];path=patch["old"]
    if not path or path!=patch["new"] or not path.startswith("sqlglot/") or path not in selector.SRC or path in selector.profile()["source_reads"]:return selected
    old=selector.SRC[path]
    new,removed,added=selector.apply(old,patch["hunks"])
    if len(removed)!=1 or len(added)!=1:return selected
    line=removed[0]
    a=old.splitlines();b=new.splitlines()
    if a[:line-1]!=b[:line-1] or a[line:]!=b[line:]:return selected
    left,right=a[line-1],b[line-1]
    if not left.isascii() or not right.isascii():return selected
    for start,end,kind,original in sites(selector,path).get(line,()):
        if not right.startswith(left[:start]) or not right.endswith(left[end:]):continue
        try:node=ast.parse(right[start:len(right)-len(left[end:])],mode="eval").body
        except (SyntaxError,ValueError):continue
        if kind in ("seq","index"):
            value=number(node)
            if value is None:continue
        else:
            if not isinstance(node,ast.Constant) or type(node.value) is not str:continue
            value=node.value
        names,records=facts()
        if set(names)!=set(selector.NUM):return selected
        rows=records.get((path,line,start))
        if rows is None:return selected
        may=rows.get(None,0)
        if kind in ("seq","index"):
            for shape,mask in rows.items():
                if shape is None:continue
                before=result(shape,original,kind);after=result(shape,value,kind)
                if before is None or before!=after:may|=mask
        else:
            may|=rows.get(original,0)|rows.get(value,0)
        keep=selector.tests_of(selector.M["opens"].get(path))|preserve_scope(selector,path,1<<line,selected)
        keep.update(i for i,(files,_) in selector.EXTRA.items() if path in files)
        return {i for i in selected if i in keep or may&names[selector.TEST_FILE_ID[i]]}
    return selected
