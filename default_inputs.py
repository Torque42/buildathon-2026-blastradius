"""Changing a literal Python default can affect calls and function introspection."""
import ast
import gzip
import json
import marshal
from functools import lru_cache
from pathlib import Path

ROOT=Path(__file__).resolve().parent

@lru_cache(maxsize=1)
def observations():
    inspected=json.loads(gzip.decompress((ROOT/"maps/default-inspection.json.gz").read_bytes()))
    known,locations=marshal.loads(gzip.decompress((ROOT/"maps/function-entries.gz").read_bytes()))
    return set(inspected["catalog"]),set(inspected["sites"]),known,locations

@lru_cache(maxsize=16)
def declarations(selector,path):
    result={}
    tree=selector.source_tree(path)
    for scope in [tree,*[n for n in tree.body if isinstance(n,ast.ClassDef)]]:
        for node in scope.body:
            if not isinstance(node,ast.FunctionDef) or node.decorator_list or node.name.startswith("__"):continue
            for value in node.args.defaults+node.args.kw_defaults:
                if isinstance(value,ast.Constant) and type(value.value) in (str,bool,int) and value.lineno==value.end_lineno:
                    result.setdefault(value.lineno,[]).append((value.col_offset,value.end_col_offset,node.lineno,type(value.value)))
    return result

def refine(selector,text,selected,reasons):
    if not selected or reasons!=["def-header"]:return selected
    files=selector.parse_patch(text)
    if len(files)!=1:return selected
    patch=files[0];path=patch["old"]
    if path is None or patch["new"]!=path or path not in selector.SRC:return selected
    original=selector.SRC[path]
    updated,removed,inserted=selector.apply(original,patch["hunks"])
    if len(removed)!=1 or len(inserted)!=1:return selected
    n=removed[0];left=original.splitlines();right=updated.splitlines()
    if n<1 or len(left)!=len(right) or left[:n-1]!=right[:n-1] or left[n:]!=right[n:]:return selected
    a,b=left[n-1],right[n-1]
    if not a.isascii() or not b.isascii():return selected
    for start,end,owner,kind in declarations(selector,path).get(n,()):
        prefix,suffix=a[:start],a[end:]
        if not b.startswith(prefix) or not b.endswith(suffix):continue
        value=b[start:len(b)-len(suffix)] if suffix else b[start:]
        try:node=ast.parse(value,mode="eval").body
        except (SyntaxError,ValueError):continue
        if not isinstance(node,ast.Constant) or type(node.value) is not kind:continue
        catalog,inspected,known,locations=observations()
        if catalog!=set(selector.NUM) or f"{path}:{owner}" in inspected:return selected
        entered=0
        for lines,ids in locations.get(path,{}).items():
            if lines & (1<<owner):entered|=ids
        keep=selector.tests_of(selector.M["opens"].get(path)) | selector.UNTRACED_POOL_TESTS
        # An absent profile for this node is uncertainty, as is direct source inspection.
        for number in selected:
            ix=known.get(selector.TEST_FILE_ID[number])
            if ix is None or entered & (1<<ix) or path in selector.EXTRA.get(number,({},{}))[0]:keep.add(number)
        return selected & keep
    return selected
