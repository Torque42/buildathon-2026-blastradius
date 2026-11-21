"""Account for executor callbacks invoked by dynamically compiled query code."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path

FILE = "sqlglot/executor/env.py"
@lru_cache(1)
def profile():
    return json.loads(gzip.decompress((Path(__file__).parent/"maps/executor-symbols.gz").read_bytes()))

@lru_cache(1)
def entries(selector):
    for node in selector.source_tree(FILE).body:
        if isinstance(node, ast.Assign) and any(isinstance(t,ast.Name) and t.id=="ENV" for t in node.targets) and isinstance(node.value,ast.Dict):
            bounds = (node.lineno,node.end_lineno)
            rows = [(k.value,min(k.lineno,v.lineno),max(k.end_lineno,v.end_lineno))
                    for k,v in zip(node.value.keys,node.value.values) if isinstance(k,ast.Constant) and type(k.value) is str]
            return bounds, rows
    return None, ()

def include(selector,text,selected):
    if FILE not in text:
        return selected
    for patch in selector.parse_patch(text):
        if patch["old"] != FILE:
            continue
        _, removed, inserted = selector.apply(selector.SRC[FILE],patch["hunks"])
        bounds, rows = entries(selector)
        if bounds is None:
            return set(selector.ALL)
        if not any(bounds[0] <= line <= bounds[1]+1 for line in [*removed,*inserted]):
            continue
        data = profile()
        if set(data["catalog"]) != set(selector.NUM):
            return set(selector.ALL)
        table = data["readers"]
        keys = {key for key,first,last in rows if any(first<=line<=last for line in removed)}
        if not keys or not removed or len(removed)!=len(inserted):
            keys = set(table)
        # Newly introduced names can alter a dynamic lookup or shadow a builtin.
        keys |= {node for h in patch["hunks"] for line in h["lines"] if line.startswith("+")
                 for node in table if repr(node) in line or ('"' + node + '"') in line}
        out = set(selected)
        for key in keys:
            for name in table.get(key, ()):
                if name in selector.NUM:
                    out.add(selector.NUM[name])
                elif name.startswith("scope:") and name[6:] in selector.BY_FILE:
                    out.update(selector.BY_FILE[name[6:]])
                else:
                    return set(selector.ALL)
        return out
    return selected
