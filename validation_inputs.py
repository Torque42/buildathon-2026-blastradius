"""Trace which tests can observe an expression argument becoming required or optional."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def evidence():
    file = Path(__file__).parent/"maps"/"validation-inputs.json.gz"
    data = json.loads(gzip.decompress(file.read_bytes()))
    return set(data["catalog"]), set(data["sites"]), data["readers"]


@lru_cache(maxsize=24)
def declarations(selector, path):
    result = {}
    for cls in selector.source_tree(path).body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.Dict):
                continue
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name) or targets[0].id != "arg_types":
                continue
            keys = stmt.value.keys
            if not all(isinstance(k, ast.Constant) and type(k.value) is str for k in keys):
                continue
            if len({key.value for key in keys}) != len(keys):
                continue
            site = path[:-3].replace("/", ".")+":"+cls.name
            for key, value in zip(keys, stmt.value.values):
                if isinstance(value, ast.Constant) and type(value.value) is bool:
                    result.setdefault(value.lineno, []).append(
                        (value.col_offset, value.end_col_offset, value.value, site, key.value))
    return result


def refine(selector, text, selected):
    if not selected or not ("True" in text and "False" in text):
        return selected
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return selected
    patch = patches[0]
    path = patch["old"]
    if not path or path != patch["new"] or not path.startswith("sqlglot/expressions/") or path not in selector.SRC:
        return selected
    old = selector.SRC[path]
    new, removed, inserted = selector.apply(old, patch["hunks"])
    if len(removed)!=1 or len(inserted)!=1:
        return selected
    line = removed[0]
    if line < 1:
        return selected
    a,b = old.splitlines(), new.splitlines()
    if len(a)!=len(b) or a[:line-1]!=b[:line-1] or a[line:]!=b[line:] or not a[line-1].isascii():
        return selected
    for start,end,value,site,key in declarations(selector,path).get(line, ()):
        original = a[line-1]
        if original[start:end]!=str(value) or b[line-1]!=original[:start]+str(not value)+original[end:]:
            continue
        catalog,sites,readers=evidence()
        if site not in sites or catalog!=set(selector.NUM):
            return selected
        bucket = readers.get(site,{})
        hits = set(bucket.get(key,())) | set(bucket.get("*",()))
        if "" in hits:
            return selected
        keep = set(selector.tests_of(selector.M["opens"].get(path)))
        for hit in hits:
            if hit.startswith("scope:"):
                file=hit[6:]
                if file not in selector.BY_FILE:return selected
                keep.update(selector.BY_FILE[file])
            elif hit in selector.NUM:
                keep.add(selector.NUM[hit])
            else:return selected
        return keep
    return selected
