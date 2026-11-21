"""Observe the original method dispatch path within SQLGlot's class hierarchy."""

import atexit
from collections import defaultdict
import csv
import os
from pathlib import Path
import sys

active = os.getenv("BR_RECEIVER_TEST")
reached = defaultdict(set)
mon = sys.monitoring
file_root = os.environ["BR_RECEIVER_SOURCE"] + os.sep + "sqlglot" + os.sep
lineage_cache = {}


def dispatch_path(cls, code):
    key = (cls, code)
    cached = lineage_cache.get(key)
    if cached is not None:
        return cached
    path = []
    for owner in type.__getattribute__(cls, "__mro__"):
        namespace = type.__getattribute__(owner, "__dict__")
        module = type.__getattribute__(owner, "__module__")
        if module == "sqlglot" or module.startswith("sqlglot."):
            qualname = type.__getattribute__(owner, "__qualname__")
            path.append((module, qualname, code.co_name))
        member = namespace.get(code.co_name)
        if isinstance(member, (classmethod, staticmethod)):
            member = member.__func__
        if isinstance(member, property):
            member = member.fget
        if getattr(member, "__code__", None) is code:
            break
    cached = tuple(path)
    lineage_cache[key] = cached
    return cached


def entered(code, offset):
    if (not code.co_filename.startswith(file_root) or not code.co_argcount or
            code.co_varnames[0] not in ("self", "cls") or "." not in code.co_qualname):
        return mon.DISABLE
    if active is None:
        return
    receiver = sys._getframe(1).f_locals[code.co_varnames[0]]
    cls = receiver if isinstance(receiver, type) else type(receiver)
    reached[active].update(dispatch_path(cls, code))


def save():
    destination = Path(os.environ["BR_RECEIVER_TRACE"]) / f"{os.getpid()}.csv"
    with destination.open("w", newline="") as stream:
        output = csv.writer(stream)
        for test, slots in sorted(reached.items()):
            output.writerows((test, *slot) for slot in sorted(slots))


def install():
    mon.use_tool_id(4, "blast-radius-method-receivers")
    mon.register_callback(4, mon.events.PY_START, entered)
    mon.set_events(4, mon.events.PY_START)
    atexit.register(save)
