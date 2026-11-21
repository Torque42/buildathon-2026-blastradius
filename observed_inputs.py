"""Match edits to observed closure reads and pure module-presence expressions."""
import ast
import gzip
import json
from functools import lru_cache
from pathlib import Path

@lru_cache(1)
def profile():
    raw = json.loads(gzip.decompress((Path(__file__).parent/"maps/observed-inputs.gz").read_bytes()))
    raw["catalog"] = set(raw["catalog"])
    raw["states"] = [set(names) for names in raw["states"]]
    return raw

def guard(node, modules):
    if isinstance(node, ast.Constant) and type(node.value) is bool:
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not guard(node.operand, modules)
    if isinstance(node, ast.BoolOp):
        values = [guard(value, modules) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare) and len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn)):
        right = node.comparators[0]
        if (isinstance(node.left, ast.Constant) and type(node.left.value) is str and isinstance(right, ast.Attribute)
            and right.attr == "modules" and isinstance(right.value, ast.Name) and right.value.id == "sys"):
            return (node.left.value in modules) != isinstance(node.ops[0], ast.NotIn)
    raise ValueError("unsupported module guard")

def refine(selector, text, selected):
    if not selected:
        return selected
    files = selector.parse_patch(text)
    if len(files) != 1:
        return selected
    patch = files[0]
    path = patch["old"]
    if not path or path != patch["new"] or path not in selector.SRC:
        return selected
    data = profile()
    candidates = data["sites"].get(path, ())
    guards = data["guard_sites"].get(path, ())
    if not candidates and not guards:
        return selected
    old = selector.SRC[path]
    new, removed, inserted = selector.apply(old, patch["hunks"])
    if len(removed) != 1 or len(inserted) != 1:
        return selected
    line = removed[0]
    lines, updated = old.splitlines(), new.splitlines()
    if line < 1 or lines[:line-1] != updated[:line-1] or lines[line:] != updated[line:]:
        return selected
    before, after = lines[line-1], updated[line-1]
    if not before.isascii() or not after.isascii():
        return selected
    for row in (*candidates, *guards):
        n, start, end = row[:3]
        if n != line or not after.startswith(before[:start]) or not after.endswith(before[end:]):
            continue
        right = len(after) - len(before[end:])
        try:
            b = ast.parse(after[start:right], mode="eval").body
        except (ValueError, SyntaxError):
            continue
        if len(row) == 6:
            _, _, _, factory, param, original = row
            if not isinstance(b, ast.Constant) or type(b.value).__name__ != original.split(":", 1)[0]:
                continue
            eligible = set(data["reads"].get(f"{factory}:{param}:{original}", ()))
        else:
            a = ast.parse(before[start:end], mode="eval").body
            eligible = set()
            runs = data["guards"].get(f"{path}:{n}:{start}", {})
            try:
                changed = {i for i, state in enumerate(data["states"]) if guard(a, state) != guard(b, state)}
            except ValueError:
                continue
            for test in selected:
                file = selector.TEST_FILE[test]
                if file not in runs or changed.intersection(runs[file]):
                    eligible.add(selector.TEST_FILE_ID[test])
        # Source inspection may observe spelling even when the runtime value is unused.
        keep = selector.tests_of(selector.M["opens"].get(path))
        for test in selected:
            name = selector.TEST_FILE_ID[test]
            if name not in data["catalog"] or name in eligible or path in selector.EXTRA.get(test, ({}, {}))[0]:
                keep.add(test)
        return (selected & keep) | ({selector.NUM[name] for name in eligible if name in selector.NUM} if len(row) == 6 else set())
    return selected
