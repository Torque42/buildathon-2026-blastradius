"""Select readers of a complete, profiled conditional removed from a function."""
import ast
import gzip
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from instruction_impact import lineage, metadata, original


def guard_spec(expr):
    negate = isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not)
    if negate:
        expr = expr.operand
    if (isinstance(expr, ast.Compare) and len(expr.ops) == 1
            and isinstance(expr.ops[0], (ast.Is, ast.IsNot)) and isinstance(expr.left, ast.Name)
            and isinstance(expr.comparators[0], ast.Constant) and expr.comparators[0].value is None):
        return ("none", expr.left.id, "", "", negate != isinstance(expr.ops[0], ast.IsNot))
    if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "isinstance"
            and not expr.keywords and len(expr.args) == 2 and isinstance(expr.args[0], ast.Name)):
        target = expr.args[1]
        if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            return ("class", expr.args[0].id, target.value.id, target.attr, negate)


def guard_sites(tree):
    """Find whole runtime guards. A nested class or function starts its own scope."""
    pending = [(tree, None)]
    while pending:
        node, owner = pending.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pending.extend((child, node) for child in node.body)
            continue
        if isinstance(node, ast.ClassDef):
            owner = None
        if isinstance(node, ast.If) and owner is not None and not node.orelse:
            spec = guard_spec(node.test)
            if spec and node.lineno == node.test.end_lineno and node.body[0].lineno > node.lineno:
                yield node, owner, spec
        pending.extend((child, owner) for child in ast.iter_child_nodes(node))


def catalog_key(names):
    return hashlib.sha256("\n".join(sorted(names)).encode()).hexdigest()


@lru_cache(1)
def facts():
    with gzip.open(Path(__file__).parent / "maps/deleted-guards.json.gz", "rt") as stream:
        return json.load(stream)


@lru_cache(24)
def index(selector, path):
    return {node.lineno: (node, owner) for node, owner, _ in guard_sites(selector.source_tree(path))}


def same_bindings(path, source, changed):
    def doc(code):
        if code.co_consts and type(code.co_consts[0]) is str:
            return code.co_consts[0]

    try:
        before = original(path, source)
        after = dict(lineage(compile(changed, path, "exec", dont_inherit=True)))
    except (SyntaxError, ValueError, OverflowError):
        return False
    if before.keys() != after.keys():
        return False
    for key, code in before.items():
        other = after[key]
        if metadata(code) != metadata(other):
            return False
        if code.co_flags & 1 and doc(code) != doc(other):
            return False
    return True


def match(selector, text):
    # Most patches have a + line or do not delete an if. They pay no syntax or map load cost.
    if not any(line.startswith("-") and line[1:].lstrip().startswith("if ") for line in text.splitlines()):
        return None
    files = selector.parse_patch(text)
    if len(files) != 1:
        return None
    patch = files[0]
    path = patch["old"]
    if not path or path != patch["new"] or not path.startswith("sqlglot/") or path not in selector.SRC:
        return None
    if any(line.startswith("+") for hunk in patch["hunks"] for line in hunk["lines"]):
        return None
    source = selector.SRC[path]
    changed, removed, added = selector.apply(source, patch["hunks"])
    if not removed or added or removed != list(range(removed[0], removed[-1] + 1)):
        return None
    start, end = removed[0], removed[-1]
    original_lines = source.splitlines()
    deleted = [line[1:] for hunk in patch["hunks"] for line in hunk["lines"] if line.startswith("-")]
    if original_lines[start - 1:end] != deleted or not original_lines[start - 1].lstrip().startswith("if "):
        return None
    if original_lines[:start - 1] + original_lines[end:] != changed.splitlines():
        return None
    data = facts()
    entry = data["sites"].get(path, {}).get(str(start))
    if not entry or catalog_key(selector.NUM) != data["catalog"]:
        return None
    if hashlib.sha256(source.replace("\r\n", "\n").encode()).hexdigest() != data["sources"].get(path):
        return None
    located = index(selector, path).get(start)
    if not located or located[0].end_lineno != end:
        return None
    node, owner = located
    if (any(isinstance(part, (ast.Global, ast.Nonlocal, ast.Yield, ast.YieldFrom, ast.ClassDef,
                              ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) for part in ast.walk(node))
            or owner.name.startswith("__") or not same_bindings(path, source, changed)):
        return None
    # Removing lines also shifts source locations in the rest of the file.
    diagnostic = selector.profile()
    if path in diagnostic["source_reads"] or any(n >= start for n in diagnostic["code_reads"].get(path, ())):
        return None
    names = data["names"]
    observed, may_change = entry
    seen = {selector.NUM[names[n]] for n in observed}
    keep = {selector.NUM[names[n]] for n in may_change}
    header = selector.cov_range(path, start, start)
    keep.update(selector.cov_range(path, start + 1, end))
    keep.update(selector.tests_of(selector.M["opens"].get(path)))
    keep.update(selector.UNTRACED_POOL_TESTS)
    keep.update(selector.UNMAPPED - selector.EXTRA.keys())
    for scope, lines in selector.FIXTURE_LINES.get(path, ()):
        if start in lines:
            keep.update(selector.scope_tests(scope))
    for number, (opened, lines) in selector.EXTRA.items():
        hits = lines.get(path, ())
        if path in opened or any(start < n <= end for n in hits):
            keep.add(number)
        if start in hits:
            header.add(number)
    return keep | (header - seen)
