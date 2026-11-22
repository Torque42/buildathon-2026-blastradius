"""Compare a rewritten guard on the operands observed at the original guard."""
import ast
from functools import lru_cache

import branch_select
from branch_model import tree, unchanged
from replay_edit import preserve_scope


def split_guard(lines, guard):
    # AST columns count UTF-8 bytes. Splitting encoded lines preserves that convention.
    start, end = guard.lineno - 1, guard.end_lineno - 1
    return (b"".join(lines[:start]) + lines[start][:guard.col_offset],
            lines[end][guard.end_col_offset:] + b"".join(lines[end + 1:]))


def same_surroundings(old, new):
    if old == new:
        return True
    # A removed whole-line comment can shift a guard. Restrict this comparison
    # to the common prefix and different trailing comment lines, never strings.
    old_lines, new_lines = old.splitlines(keepends=True), new.splitlines(keepends=True)
    while old_lines and new_lines and old_lines[-1] == new_lines[-1]:
        old_lines.pop()
        new_lines.pop()
    i = 0
    while i < min(len(old_lines), len(new_lines)) and old_lines[i] == new_lines[i]:
        i += 1
    return all(row.lstrip().startswith(b"#") for row in old_lines[i:] + new_lines[i:])


def guards(module, start, end):
    for node in ast.walk(module):
        if isinstance(node, (ast.If, ast.While, ast.IfExp)):
            cond = node.test
            if start <= cond.end_lineno and cond.lineno <= end:
                yield type(node), cond


@lru_cache(12)
def identify(selector, text):
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return
    patch = patches[0]
    path = patch["old"]
    if not path or path != patch["new"] or path not in selector.SRC or not path.startswith("sqlglot/"):
        return
    raw = [line[1:] for h in patch["hunks"] for line in h["lines"] if line.startswith(("+", "-"))]
    if not 1 <= len(raw) <= 28 or not any(" and " in line or " or " in line for line in raw):
        return
    if path in selector.profile()["source_reads"]:
        return
    source = selector.SRC[path]
    updated, removed, inserted = selector.apply(source, patch["hunks"])
    added = selector.added_lines(patch["hunks"])
    if not removed or not added:
        return
    # Other branch refinements already cover same-width one-token substitutions.
    if len(removed) == len(added) == 1 and selector.simple_token_change(source.splitlines()[removed[0]-1], updated.splitlines()[min(added)-1]):
        return
    old = source.replace("\r\n", "\n").encode().splitlines(keepends=True)
    new = updated.replace("\r\n", "\n").encode().splitlines(keepends=True)
    left = list(guards(selector.source_tree(path), min(removed), max(removed)))
    if not left:
        return
    new_tree = ast.parse(updated)
    for kind, b in guards(new_tree, min(added), max(added)):
        for old_kind, a in left:
            if kind != old_kind or not branch_select.supported(a) or not branch_select.supported(b):
                continue
            leaves = []
            before = tree(a, leaves)
            arity = len(leaves)
            after = b.value if isinstance(b, ast.Constant) and type(b.value) is bool else tree(b, leaves)
            if len(leaves) != arity or not 1 <= arity <= 6 or before == after:
                continue
            prefix, suffix = split_guard(old, a)
            new_prefix, new_suffix = split_guard(new, b)
            if suffix != new_suffix or not same_surroundings(prefix, new_prefix):
                continue
            # Compare the whole AST around the hole if any non-guard source changed,
            # which also distinguishes a comment-looking line inside a triple-quoted string.
            if prefix != new_prefix:
                placeholder = ast.Constant(None)
                original_copy = ast.parse(source)
                original_node = next(n for n in ast.walk(original_copy)
                    if isinstance(n, kind) and (n.test.lineno, n.test.col_offset) == (a.lineno, a.col_offset))
                updated_node = next(n for n in ast.walk(new_tree) if isinstance(n, kind) and n.test is b)
                original_node.test = updated_node.test = placeholder
                same = ast.dump(original_copy) == ast.dump(new_tree)
                updated_node.test = b
                if not same:
                    continue
            return (path, a.lineno, a.col_offset), before, after, arity, a.end_lineno


def refine(selector, text, selected):
    if not selected:
        return selected
    proof = identify(selector, text)
    if not proof:
        return selected
    site, before, after, arity, end = proof
    path, start, _ = site
    import collection_guard
    import import_impact
    mask = ((1 << (end-start+1))-1) << start
    if mask & collection_guard.inputs()[1].get(path, 0) or any(
            mask & (1 << n) for n in import_impact.profile()[0].get(path, {})):
        return selected
    bits, data = branch_select.facts()
    if set(bits) != set(selector.NUM) or site not in data:
        return selected
    possible = 0
    for state, tests in data[site].items():
        if state is None or len(state) != arity or not unchanged(before, after, state):
            possible |= tests
    inspected = sum(1 << line for line in set(selector.profile()["code_reads"].get(path, ())))
    keep = preserve_scope(selector, path, inspected | mask, selected) | selector.tests_of(selector.M["opens"].get(path))
    keep.update(n for n, (files, _) in selector.EXTRA.items() if path in files)
    return {n for n in selected if n in keep or bits[selector.TEST_FILE_ID[n]] & possible}
