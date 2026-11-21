"""Resolve literal class-setting edits to their observed pristine readers."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path
import re


@lru_cache(1)
def observations():
    data=json.load(gzip.open(Path(__file__).parent/"maps"/"flags.json.gz","rt"))
    return set(data["catalog"]),set(data["sites"]),data["readers"]


def scalar(node):
    try:value=ast.literal_eval(node)
    except (TypeError,ValueError):return None
    return (type(value),value) if value is None or type(value) in (int,str,bool) else None


@lru_cache(256)
def source_slots(selector,path):
    result={}
    for cls in selector.source_tree(path).body:
        if not isinstance(cls,ast.ClassDef):continue
        for stmt in cls.body:
            if isinstance(stmt,ast.Assign) and len(stmt.targets)==1:
                target=stmt.targets[0]
            elif isinstance(stmt,ast.AnnAssign):
                target=stmt.target
            else:continue
            value=stmt.value
            if not isinstance(target,ast.Name) or not target.id.isupper() or scalar(value) is None or stmt.lineno!=stmt.end_lineno:
                continue
            # A bare read inside the class can copy a value before descriptors
            # are installed. Likewise, metaclasses can consume the namespace.
            name=target.id
            if any(isinstance(n,ast.Name) and n.id==name and isinstance(n.ctx,ast.Load) for n in ast.walk(cls)):
                continue
            if raw_or_construction_read(selector,name):continue
            result[stmt.lineno]=(name,cls.name,value.col_offset,value.end_col_offset,scalar(value)[0])
    return result


@lru_cache(1)
def construction_names(selector):
    found=set()
    for path,src in selector.SRC.items():
        if not path.endswith(".py") or not path.startswith(("sqlglot/","tests/")):continue
        for line in src.splitlines():
            if any(word in line for word in ("__dict__", "getattr(", "vars(", "attrs.get(", "attrs[")):
                found.update(re.findall(r'["\']([A-Z][A-Z_0-9]*)["\']',line))
        if not path.startswith("sqlglot/") or not any(w in src for w in ("def __init_subclass__", "def __new__")):continue
        for fn in ast.walk(selector.source_tree(path)):
            if isinstance(fn,ast.FunctionDef) and fn.name in ("__init_subclass__","__new__"):
                found.update(n.attr for n in ast.walk(fn) if isinstance(n,ast.Attribute))
                found.update(n.value for n in ast.walk(fn) if isinstance(n,ast.Constant) and isinstance(n.value,str))
    return found

def raw_or_construction_read(selector,name):
    return name in construction_names(selector)

def refine(selector,text,selected):
    files=selector.parse_patch(text)
    if len(files)!=1:return selected
    f=files[0];path=f["old"]
    if path!=f["new"] or not path or not path.startswith(("sqlglot/parsers/","sqlglot/generators/","sqlglot/dialects/")) or path not in selector.SRC:
        return selected
    src=selector.SRC[path]
    new,removed,inserted=selector.apply(src,f["hunks"])
    if len(removed)!=1 or len(inserted)!=1:return selected
    line=removed[0]
    before=src.splitlines();after=new.splitlines()
    if before[:line-1]!=after[:line-1] or before[line:]!=after[line:]:return selected
    if not re.match(r"^    [A-Z][A-Z_0-9]*\s*[:=]",before[line-1]):return selected
    slot=source_slots(selector,path).get(line)
    if not slot:return selected
    name,owner,start,end,original_type=slot
    a,b=before[line-1],after[line-1]
    if not a.isascii() or not b.startswith(a[:start]) or not b.endswith(a[end:]):return selected
    right=len(b)-len(a[end:])
    try:expr=ast.parse(b[start:right],mode="eval")
    except SyntaxError:return selected
    v=scalar(expr.body)
    if v is None or v[0]!=original_type:return selected
    catalog,sites,readers=observations();site=f"{path}:{owner}:{name}"
    if site not in sites or set(selector.NUM)!=catalog:return selected
    ids=readers.get(site,())
    if any(key not in selector.NUM for key in ids):return selected
    if path in selector.profile()["source_reads"] or line in selector.profile()["code_reads"].get(path,()):return selected
    return {selector.NUM[key] for key in ids} | selector.tests_of(selector.M["opens"].get(path))
