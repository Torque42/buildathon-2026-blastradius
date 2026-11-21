"""Follow the old and new lookup keys through inherited parser dispatch dictionaries."""
import ast
import gzip
import json
import marshal
from functools import lru_cache
from pathlib import Path

TABLES = {"FUNCTIONS", "FUNCTION_PARSERS", "NO_PAREN_FUNCTION_PARSERS"}

@lru_cache(1)
def profile():
    record = json.loads(gzip.decompress((Path(__file__).parent/"maps/parser-table-keys.gz").read_bytes()))
    return set(record["catalog"]), record["rows"]

def collect_entries(path, tree):
    locations = {}
    for cls in tree.body:
        if not isinstance(cls,ast.ClassDef):
            continue
        for stmt in cls.body:
            if isinstance(stmt,ast.Assign) and len(stmt.targets)==1:
                target=stmt.targets[0]
            elif isinstance(stmt,ast.AnnAssign):
                target=stmt.target
            else:
                continue
            if not isinstance(target,ast.Name) or target.id not in TABLES or not isinstance(stmt.value,ast.Dict):
                continue
            if any(isinstance(node,ast.Name) and isinstance(node.ctx,ast.Load) and node.id==target.id for node in ast.walk(cls)):
                continue
            for key,value in zip(stmt.value.keys,stmt.value.values):
                if not isinstance(key,ast.Constant) or type(key.value) is not str or key.lineno!=key.end_lineno:
                    continue
                site=path[:-3].replace("/",".")+":"+cls.name+":"+target.id
                locations.setdefault(key.lineno,[]).append((site,key.value,"key",key.col_offset,key.end_col_offset))
                if isinstance(value,ast.Lambda) and value.body.lineno==value.body.end_lineno:
                    body=value.body
                    locations.setdefault(body.lineno,[]).append((site,key.value,"callback",body.col_offset,body.end_col_offset))
    return locations

@lru_cache(1)
def entry_index():
    cache=Path(__file__).parent/"cache/parser-table-entries"
    return marshal.loads(cache.read_bytes()) if cache.is_file() else None

@lru_cache(32)
def entries(selector,path):
    index=entry_index()
    if index is not None:
        return index.get(path,{})
    return collect_entries(path,selector.source_tree(path))

def refine(selector,text,selected):
    files=selector.parse_patch(text)
    if len(files)!=1:
        return selected
    patch=files[0]
    path=patch["old"]
    if not path or path!=patch["new"] or not (path=="sqlglot/parser.py" or path.startswith("sqlglot/parsers/")) or path not in selector.SRC:
        return selected
    original=selector.SRC[path]
    updated,removed,added=selector.apply(original,patch["hunks"])
    if len(removed)!=1 or len(added)!=1:
        return selected
    line=removed[0]
    aa,bb=original.splitlines(),updated.splitlines()
    if aa[:line-1]!=bb[:line-1] or aa[line:]!=bb[line:] or not aa[line-1].isascii() or not bb[line-1].isascii():
        return selected
    left,right=aa[line-1],bb[line-1]
    for site,key,mode,start,end in entries(selector,path).get(line,()):
        if not right.startswith(left[:start]) or not right.endswith(left[end:]):
            continue
        edited=right[start:len(right)-len(left[end:])]
        if mode=="key":
            try:
                expr=ast.parse(edited,mode="eval").body
            except (SyntaxError,ValueError):
                continue
            if not isinstance(expr,ast.Constant) or type(expr.value) is not str or not expr.value.isidentifier() or not key.isidentifier():
                continue
            keys={key,expr.value}
        else:
            # A one-token literal or comparator inside a lambda cannot change the
            # class body or its function defaults when constructing the dispatch table.
            if not selector.simple_token_change(left,right):
                continue
            keys={key}
        catalog, rows=profile()
        if set(selector.NUM)!=catalog or site not in rows or path in selector.profile()["source_reads"]:
            return selected
        from replay_edit import preserve_scope
        keep=selector.tests_of(selector.M["opens"].get(path)) | selector.UNTRACED_POOL_TESTS
        keep.update(preserve_scope(selector,path,1<<line,selector.ALL))
        keep.update(n for n,(raw,_) in selector.EXTRA.items() if path in raw)
        seen=set()
        stack=[site]
        while stack:
            current=stack.pop()
            if current in seen:
                continue
            seen.add(current)
            if current not in rows:
                return selected
            table=rows[current]
            stack.extend(table.get("@copy",()))
            for lookup in keys | {"*"}:
                for context in table.get(lookup,()):
                    if context in selector.NUM:
                        keep.add(selector.NUM[context])
                    elif context.startswith("scope:") and context[6:] in selector.BY_FILE:
                        keep.update(selector.BY_FILE[context[6:]])
                    elif context.startswith(("import:","test-import:")) or not context:
                        # A lookup during collection or module initialization can
                        # change registrations or make additional files fail to import.
                        return selected | selector.ALL
                    else:
                        return selected
        return keep
    return selected
