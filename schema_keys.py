"""Select observed consumers of a changed expression-class argument dictionary."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path

@lru_cache(1)
def profile():
    source = Path(__file__).parent / "maps/schema-readers.gz"
    payload = json.loads(gzip.decompress(source.read_bytes()))
    return set(payload["catalog"]), payload["rows"]

def schema(node):
    if not isinstance(node, ast.ClassDef):
        return
    for stmt in node.body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
        elif isinstance(stmt, ast.AnnAssign):
            target = stmt.target
        else:
            continue
        if not isinstance(target, ast.Name) or target.id != "arg_types":
            continue
        values = stmt.value
        if not isinstance(values, ast.Dict):
            return
        if not all(isinstance(key, ast.Constant) and type(key.value) is str for key in values.keys):
            return
        if not all(isinstance(value, ast.Constant) and type(value.value) is bool for value in values.values):
            return
        pairs = tuple((key.value, value.value) for key, value in zip(values.keys, values.values))
        if len(dict(pairs)) != len(pairs):
            return
        return values, pairs

def refine(selector, text, selected):
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return selected
    patch = patches[0]
    path = patch["old"]
    if not path or path != patch["new"] or not path.startswith("sqlglot/expressions/") or path not in selector.SRC:
        return selected
    before = selector.SRC[path]
    after, removed, inserted = selector.apply(before, patch["hunks"])
    if not removed or not inserted or len(removed) + len(inserted) > 20:
        return selected
    tree = selector.source_tree(path)
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef) or not cls.lineno <= min(removed) <= cls.end_lineno:
            continue
        original = schema(cls)
        if not original or not all(original[0].lineno <= n <= original[0].end_lineno for n in removed):
            return selected
        edited = ast.parse(after)
        updated_class = next((n for n in edited.body if isinstance(n, ast.ClassDef) and n.name == cls.name), None)
        revised = schema(updated_class)
        if not revised:
            return selected
        old_value, old_pairs = original
        new_value, new_pairs = revised
        # A boolean-only change has its own finer validation/required_args profile.
        if [k for k, _ in old_pairs] == [k for k, _ in new_pairs]:
            return selected
        # Verify that this dictionary is the sole source difference. AST equality
        # alone could hide edits to comments or to text read by another module.
        def outside(source, node):
            lines = source.replace("\r\n", "\n").splitlines(keepends=True)
            return ("".join(lines[:node.lineno-1]).encode()+lines[node.lineno-1].encode()[:node.col_offset],
                    lines[node.end_lineno-1].encode()[node.end_col_offset:]+"".join(lines[node.end_lineno:]).encode())
        if outside(before, old_value) != outside(after, new_value):
            return selected
        catalog, rows = profile()
        site = path[:-3].replace("/", ".")+":"+cls.name
        if catalog != set(selector.NUM) or site not in rows or path in selector.profile()["source_reads"]:
            return selected
        # Include every observed reader even if the baseline originally missed it.
        keep = set(selector.tests_of(selector.M["opens"].get(path))) | selector.UNTRACED_POOL_TESTS
        keep.update(n for n, (raw, _) in selector.EXTRA.items() if path in raw)
        from replay_edit import preserve_scope
        keep.update(preserve_scope(selector, path, sum(1 << n for n in range(old_value.lineno, old_value.end_lineno+1)), selector.ALL))
        for context in rows[site]:
            if context in selector.NUM:
                keep.add(selector.NUM[context])
            elif context.startswith("scope:") and context[6:] in selector.BY_FILE:
                keep.update(selector.BY_FILE[context[6:]])
            elif context.startswith("test-import:") and context[12:] in selector.BY_FILE:
                keep.update(selector.BY_FILE[context[12:]])
                keep.update(selector.dependent_test_numbers(context[12:]))
            elif context.startswith("import:"):
                from isolated_imports import readers
                keep.update(readers(selector, context[7:]))
            else:
                return set(selector.ALL)
        return keep
    return selected
