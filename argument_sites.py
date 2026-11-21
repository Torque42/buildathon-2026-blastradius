"""Locate immutable argument literals whose reads can be measured on the pin."""
import ast
from functools import lru_cache

SCALAR = (type(None), bool, int, str)


def literal(node):
    if isinstance(node, ast.Constant) and type(node.value) in SCALAR:
        return True, node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd))
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) is int):
        return True, ast.literal_eval(node)
    return False, None


def locate(path, source):
    calls, defaults, lines = {}, {}, {}
    tree = ast.parse(source)

    def site(node, kind):
        if node.lineno != node.end_lineno or not literal(node)[0]:
            return None
        key = (path, node.lineno, node.col_offset, node.end_col_offset, kind)
        lines.setdefault(node.lineno, []).append(key)
        return key

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and not any(kw.arg is None for kw in node.keywords):
            entries = {}
            for kw in node.keywords:
                key = site(kw.value, "call")
                if key:
                    entries[kw.arg] = key
            if entries:
                calls[node.lineno, node.col_offset, node.end_lineno, node.end_col_offset] = entries
        elif isinstance(node, ast.FunctionDef) and not node.decorator_list:
            args = node.args.posonlyargs + node.args.args
            params = list(zip(args[len(args)-len(node.args.defaults):], node.args.defaults))
            params += list(zip(node.args.kwonlyargs, node.args.kw_defaults))
            entries = {}
            for arg, val in params:
                if val is None:
                    continue
                key = site(val, "default")
                if key:
                    entries[arg.arg] = (key, literal(val)[1])
            if entries:
                defaults[node.lineno, node.name] = entries
    return calls, defaults, lines
