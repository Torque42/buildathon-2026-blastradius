"""Evaluate a tuple of simple row expressions in a single Python evaluation."""
import ast
from functools import lru_cache
from types import CodeType

ALLOWED = (ast.Expression, ast.Name, ast.Load, ast.Constant, ast.Subscript, ast.Slice,
           ast.Tuple, ast.List, ast.BoolOp, ast.UnaryOp, ast.BinOp, ast.Compare, ast.Call,
           ast.And, ast.Or, ast.Not, ast.USub, ast.UAdd, ast.Add, ast.Sub, ast.Mult,
           ast.Div, ast.Mod, ast.Eq, ast.NotEq, ast.Is, ast.IsNot, ast.In, ast.NotIn,
           ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.IfExp)

@lru_cache(maxsize=2048)
def combine(codes):
    if not codes or len(codes) > 120 or not all(type(c) is CodeType for c in codes):
        return None
    values = []
    for code in codes:
        source = code.co_filename
        if len(source) > 3000:
            return None
        try:
            expression = ast.parse(source, mode="eval")
            check = compile(source, source, "eval", dont_inherit=True, optimize=2)
        except (SyntaxError, ValueError, OverflowError):
            return None
        if (check.co_code != code.co_code or check.co_names != code.co_names
                or check.co_consts != code.co_consts or check.co_freevars or code.co_freevars):
            return None
        for node in ast.walk(expression):
            if not isinstance(node, ALLOWED):
                return None
            if isinstance(node, ast.Name) and node.id != "scope" and not node.id.isupper():
                return None
            if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name)
                                              or not node.func.id.isupper() or node.keywords):
                return None
        values.append(expression.body)
    tree = ast.fix_missing_locations(ast.Expression(ast.Tuple(elts=values, ctx=ast.Load())))
    return compile(tree, "<row projection>", "eval", dont_inherit=True, optimize=2)

def install(edits):
    # This fast path relies on the pinned Context.eval and Context.eval_tuple
    # implementations. The producer of each program still runs on patched code.
    if not all(path.startswith("sqlglot/optimizer/") for path, _, _ in edits):
        return
    from sqlglot.executor.context import Context
    original = Context.eval_tuple
    def evaluate(self, codes):
        if type(codes) is not tuple:
            return original(self, codes)
        if not codes:
            return ()
        last = getattr(self, "_br_projection", None)
        if last is None or last[0] is not codes:
            last = (codes, combine(codes))
            self._br_projection = last
        program = last[1]
        if program is None:
            return original(self, codes)
        return eval(program, self.env)
    Context.eval_tuple = evaluate
