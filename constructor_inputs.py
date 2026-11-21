"""Select tests that can observe a changed literal constructor default on the pin."""
import ast
from functools import lru_cache
import json
from pathlib import Path

import default_inputs
import replay_edit


AUDITED = {
    "sqlglot/parser.py": ("Parser", {"max_errors", "max_nodes", "error_message_context"}),
    "sqlglot/planner.py": ("SetOperation", {"distinct"}),
}


def literal_type(node):
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        return int if isinstance(node.operand, ast.Constant) and type(node.operand.value) is int else None
    if isinstance(node, ast.Constant) and type(node.value) in (int, bool):
        return type(node.value)
    return None


@lru_cache(maxsize=2)
def parameters(selector, path):
    owner, fields = AUDITED[path]
    cls = next(node for node in selector.source_tree(path).body
               if isinstance(node, ast.ClassDef) and node.name == owner)
    init = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "__init__")
    if init.decorator_list:
        return {}
    args = init.args.posonlyargs + init.args.args
    result = {}
    for arg, value in zip(args[-len(init.args.defaults):], init.args.defaults):
        if arg.arg not in fields or literal_type(value) is None:
            continue
        if value.lineno != value.end_lineno:
            continue
        if owner == "Parser":
            # The incoming value must flow only into a slot. Any second use of
            # the argument, including an expression in a nested function, blocks pruning.
            uses = [child for statement in init.body for child in ast.walk(statement)
                    if isinstance(child, ast.Name) and child.id == arg.arg]
            stores = [stmt for stmt in init.body if isinstance(stmt, ast.AnnAssign)
                      and isinstance(stmt.target, ast.Attribute) and stmt.target.attr == arg.arg
                      and isinstance(stmt.target.value, ast.Name) and stmt.target.value.id == "self"
                      and isinstance(stmt.value, ast.Name) and stmt.value.id == arg.arg]
            if len(uses) != 1 or len(stores) != 1:
                continue
        result.setdefault(value.lineno, []).append((arg.arg, value, init.lineno))
    return result


@lru_cache(maxsize=1)
def observations():
    return json.loads((Path(__file__).parent / "maps/constructor-observations.json").read_bytes())


def refine(selector, text, selected):
    if not selected:
        return selected
    files = selector.parse_patch(text)
    if len(files) != 1:
        return selected
    patch = files[0]
    path = patch["old"]
    if path not in AUDITED or path != patch["new"] or path in selector.profile()["source_reads"]:
        return selected
    source = selector.SRC[path]
    updated, removed, added = selector.apply(source, patch["hunks"])
    if len(removed) != 1 or len(added) != 1:
        return selected
    line = removed[0]
    sites = parameters(selector, path).get(line, ())
    if not sites:
        return selected
    before, after = source.splitlines(), updated.splitlines()
    if before[:line-1] != after[:line-1] or before[line:] != after[line:]:
        return selected
    a, b = before[line-1], after[line-1]
    if not a.isascii() or not b.isascii():
        return selected
    for parameter, node, owner_line in sites:
        prefix, suffix = a[:node.col_offset], a[node.end_col_offset:]
        if not b.startswith(prefix) or not b.endswith(suffix):
            continue
        try:
            replacement = ast.parse(b[len(prefix):len(b)-len(suffix)], mode="eval").body
        except (ValueError, SyntaxError):
            continue
        if literal_type(replacement) is not literal_type(node):
            continue
        catalog, inspected, _, _ = default_inputs.observations()
        if catalog != set(selector.NUM) or f"{path}:{owner_line}" in inspected:
            return selected
        owner = AUDITED[path][0]
        key = f"{owner}.{parameter}"
        protected = replay_edit.preserve_scope(selector, path, 1 << line, selected)
        protected |= selector.tests_of(selector.M["opens"].get(path))
        safe = set()
        for name, trace in observations().items():
            number = selector.NUM.get(name)
            if number not in selected or number in protected or path in selector.EXTRA.get(number, ({}, {}))[0]:
                continue
            if owner not in trace["installed"] or key in trace["uncertain"]:
                continue
            if key not in trace["omitted"] or (owner == "Parser" and key not in trace["read"]):
                safe.add(number)
        return selected - safe
    return selected
