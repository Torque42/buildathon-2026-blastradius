"""Retain observed readers of a changed immutable call argument or scalar default."""
import ast
import gzip
import marshal
from functools import lru_cache
from pathlib import Path

from argument_sites import literal
from replay_edit import preserve_scope

ROOT = Path(__file__).resolve().parent

@lru_cache(1)
def profile():
    catalog, contexts, sites = marshal.loads(gzip.decompress((ROOT/"maps/argument-reads.gz").read_bytes()))
    return set(catalog), contexts, sites

@lru_cache(16)
def declarations(selector, path):
    functions = {}
    for node in ast.walk(selector.source_tree(path)):
        if not isinstance(node, ast.FunctionDef) or node.decorator_list:
            continue
        for value in node.args.defaults + node.args.kw_defaults:
            if value is not None:
                functions[value.lineno, value.col_offset] = node.lineno
    return functions

def match(selector, text):
    edits = selector.parse_patch(text)
    if len(edits) != 1:
        return
    patch = edits[0]
    path = patch["old"]
    if (not path or path != patch["new"] or path not in selector.SRC or
            not path.startswith("sqlglot/") or path in selector.profile()["source_reads"]):
        return
    catalog, contexts, sites = profile()
    if path not in sites or catalog != set(selector.NUM):
        return
    old = selector.SRC[path]
    new, removed, added = selector.apply(old, patch["hunks"])
    if len(removed) != 1 or len(added) != 1:
        return
    line = removed[0]
    entries = sites[path].get(line)
    if not entries:
        return
    left, right = old.splitlines(), new.splitlines()
    if (len(left) != len(right) or left[:line-1] != right[:line-1] or left[line:] != right[line:]
            or not left[line-1].isascii() or not right[line-1].isascii()):
        return
    first, second = left[line-1], right[line-1]
    for start, end, kind, mask in entries:
        suffix = first[end:]
        if not second.startswith(first[:start]) or not second.endswith(suffix):
            continue
        try:
            new_node = ast.parse(second[start:len(second)-len(suffix)], mode="eval").body
            original = ast.parse(first[start:end], mode="eval").body
        except (ValueError, SyntaxError):
            continue
        valid, value = literal(new_node)
        if not valid or type(value) is not type(literal(original)[1]):
            continue
        if kind == "default":
            import default_inputs
            owner = declarations(selector, path).get((line, start))
            if owner is None or f"{path}:{owner}" in default_inputs.observations()[1]:
                continue
        readers = set()
        while mask:
            bit = mask & -mask
            mask -= bit
            context = contexts[bit.bit_length()-1]
            if context in selector.NUM:
                readers.add(selector.NUM[context])
            elif context.startswith("scope:") and context[6:] in selector.BY_FILE:
                readers.update(selector.BY_FILE[context[6:]])
            else:
                return
        readers.update(selector.tests_of(selector.M["opens"].get(path)))
        readers.update(n for n, (opened, _) in selector.EXTRA.items() if path in opened)
        readers.update(selector.UNTRACED_POOL_TESTS)
        readers.update(preserve_scope(selector, path, 1 << line, selector.ALL))
        return readers
