"""Describe the complete result and inputs of an independently repeatable parse."""
import hashlib
import json
import sys


def dialect_spec(instance):
    cls = type(instance)
    if not cls.__module__.startswith("sqlglot.dialects.") or cls.__qualname__ != cls.__name__:
        return
    fresh = cls()
    # A custom instance, plugin, or test-installed method can have hidden inputs.
    if vars(fresh) != vars(instance) or any(type(key) is not str for key in vars(instance)):
        return
    return cls.__module__, cls.__name__


def parse_key(dialect, sql, opts):
    if type(sql) is not str or len(sql) > 15000:
        return
    if any(type(value) not in (type(None), int, bool, str) for value in opts.values()):
        return
    kind = dialect_spec(dialect)
    if kind is not None:
        return kind, sql, tuple(sorted(opts.items()))


def result_digest(value):
    if type(value) is not list:
        return
    # The pinned serializer includes comments, type information and all source
    # positions. An omitted or unserializable field disables the proof.
    from sqlglot import exp
    if not all(item is None or isinstance(item, exp.Expr) for item in value):
        return
    seen = set()
    details = []
    todo = [(item, None, None, None) for item in value if item is not None]
    while todo:
        node, parent, key, index = todo.pop()
        if (id(node) in seen or node.parent is not parent or node.arg_key != key or node.index != index
                or node._hash is not None or getattr(node, "__dict__", None)):
            return
        seen.add(id(node))
        details.append((node._meta is None, node.comments is None,
                        tuple((k, type(v).__name__, len(v) if type(v) is list else None) for k,v in node.args.items())))
        for field, arg in node.args.items():
            if isinstance(arg, exp.Expr):
                todo.append((arg, node, field, None))
            elif type(arg) is list:
                todo.extend((item,node,field,i) for i,item in enumerate(arg) if isinstance(item,exp.Expr))
    payload = [[item.dump() if item is not None else None for item in value], details]
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).digest()
