"""Observations for a completed pinned SQLGlot operation."""
import hashlib
import json


def signature(value):
    from sqlglot import exp
    if isinstance(value, exp.Expr):
        return value.dump()
    if isinstance(value, (list, tuple)):
        return [signature(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    raise TypeError(type(value).__name__)


def digest(result, args):
    from sqlglot import exp
    # Rules may mutate or replace their argument. Observe both, including
    # type annotations, comments and metadata carried by serializable nodes.
    data = [signature(result), [signature(arg) for arg in args if isinstance(arg, exp.Expr)]]
    return hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
