"""Certify identity/equality edits with observed operand types on the pristine pin."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path

@lru_cache(1)
def profile():
    root=Path(__file__).parent
    cached=root/"cache/identities"
    record=json.loads(cached.read_bytes() if cached.is_file() else gzip.decompress((root/"maps/identities.gz").read_bytes()))
    return set(record["catalog"]),record["reads"]


@lru_cache(128)
def locations(selector,path):
    locations={}
    for n in ast.walk(selector.source_tree(path)):
        if not isinstance(n,ast.Compare) or len(n.ops)!=1 or not isinstance(n.ops[0],(ast.Is,ast.IsNot)):continue
        right=n.comparators[0]
        if n.left.end_lineno!=right.lineno:continue
        locations.setdefault(right.lineno,[]).append((n.left.end_col_offset,right.col_offset,
            "==" if isinstance(n.ops[0],ast.Is) else "!=",f"{path}:{n.lineno}:{n.col_offset}"))
    return locations


def equivalent(shape):
    left,right,same_type,with_none,identical=shape
    if with_none:
        return left!="custom" and right!="custom"
    if left==right=="object":return True
    if left==right=="scalar-enum" and same_type:return True
    return left==right=="guarded-expression" and (identical or not same_type)


def refine(selector,text,selected):
    if not selected or " is " not in text or not any(op in text for op in ("==","!=")):return selected
    files=selector.parse_patch(text)
    if len(files)!=1:return selected
    f=files[0];path=f["old"]
    if path!=f["new"] or path not in selector.SRC or not path.startswith("sqlglot/"):return selected
    old=selector.SRC[path];new,removed,inserted=selector.apply(old,f["hunks"])
    if len(removed)!=1 or len(inserted)!=1:return selected
    line=removed[0];aa=old.splitlines();bb=new.splitlines()
    if aa[:line-1]!=bb[:line-1] or aa[line:]!=bb[line:]:return selected
    a,b=aa[line-1],bb[line-1]
    if not a.isascii() or not b.isascii() or line not in selector.FACTS.get("statements",{}).get(path,{}):return selected
    if path in selector.profile()["source_reads"]:return selected
    for start,end,op,key in locations(selector,path).get(line,()):
        right=len(b)-len(a[end:])
        if not b.startswith(a[:start]) or not b.endswith(a[end:]) or b[start:right].strip()!=op:continue
        catalog,rows=profile();observations=rows.get(key)
        if set(selector.NUM)!=catalog or not observations:return selected
        if all(equivalent(shape) for shape,ids in observations):
            from replay_edit import preserve_scope
            return selector.tests_of(selector.M["opens"].get(path)) | preserve_scope(selector,path,1<<line,selected)
    return selected
