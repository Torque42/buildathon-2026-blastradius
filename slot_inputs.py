"""Select readers of a scalar written during construction or reset, before its next overwrite."""
import ast
from functools import lru_cache
import gzip
import json
from pathlib import Path

from replay_edit import preserve_scope

OWNERS = {"sqlglot/parser.py": "Parser", "sqlglot/tokenizer_core.py": "TokenizerCore"}


@lru_cache(maxsize=1)
def profile():
    path = Path(__file__).parent / "maps/slot-reads.gz"
    payload = json.loads(gzip.decompress(path.read_bytes()))
    return set(payload["catalog"]), payload["sites"]


def scalar(node):
    if isinstance(node, ast.Constant) and type(node.value) in (bool, int, str):
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd))
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int):
        return ast.literal_eval(node)
    return None


@lru_cache(maxsize=2)
def writes(selector, path):
    owner = next(n for n in selector.source_tree(path).body if isinstance(n, ast.ClassDef) and n.name == OWNERS[path])
    module = path[:-3].replace("/", ".")
    sites = {}
    for method in owner.body:
        if not isinstance(method, ast.FunctionDef) or method.name not in ("__init__", "reset"):
            continue
        for stmt in method.body:
            if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
                target = stmt.targets[0]
            elif isinstance(stmt, ast.AnnAssign):
                target = stmt.target
            else:
                continue
            if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name) or target.value.id != "self":
                continue
            value = scalar(stmt.value)
            if value is None or stmt.lineno != stmt.end_lineno:
                continue
            sites[stmt.lineno] = (f"{module}:{stmt.lineno}:{target.attr}", stmt.value.col_offset,
                                  stmt.value.end_col_offset, type(value))
    return sites


def refine(selector, text, selected):
    if not selected or not any(path in text for path in OWNERS):
        return selected
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return selected
    patch = patches[0]
    path = patch["old"]
    if path != patch["new"] or path not in OWNERS or path in selector.profile()["source_reads"]:
        return selected
    original = selector.SRC[path]
    updated, removed, added = selector.apply(original, patch["hunks"])
    if len(removed) != 1 or len(added) != 1:
        return selected
    number = removed[0]
    site = writes(selector, path).get(number)
    if not site:
        return selected
    key, left, right, kind = site
    old, new = original.splitlines(), updated.splitlines()
    if old[:number-1] != new[:number-1] or old[number:] != new[number:]:
        return selected
    first, second = old[number-1], new[number-1]
    if not first.isascii() or not second.isascii():
        return selected
    suffix = first[right:]
    if second[:left] != first[:left] or not second.endswith(suffix):
        return selected
    try:
        literal = ast.parse(second[left:len(second)-len(suffix)], mode="eval").body
    except (SyntaxError, ValueError):
        return selected
    if type(scalar(literal)) is not kind:
        return selected
    catalog, reads = profile()
    if catalog != set(selector.NUM) or key not in reads:
        return selected
    keep = selector.tests_of(selector.M["opens"].get(path)) | selector.UNTRACED_POOL_TESTS
    keep.update(n for n, (raw, _) in selector.EXTRA.items() if path in raw)
    keep.update(preserve_scope(selector, path, 1 << number, selected))
    for context in reads[key]:
        if context in selector.NUM:
            keep.add(selector.NUM[context])
        elif context.startswith("scope:") and context[6:] in selector.BY_FILE:
            keep.update(selector.BY_FILE[context[6:]])
        else:
            return selected
    return selected & keep
