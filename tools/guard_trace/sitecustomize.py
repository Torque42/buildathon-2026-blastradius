"""Observe unmodified pristine bytecode. Unknown lookups and type protocols stay selected."""
import atexit
import builtins
import json
import os
import sys
import types
from pathlib import Path


ROOT = Path(os.environ["BR_GUARD_OUT"])
PREFIX = os.environ["BR_GUARD_SOURCE"].rstrip("/") + "/"
manifest = json.loads((ROOT / "manifest.json").read_bytes())
sites = {(PREFIX + path, line): (i, owner, spec) for i, (path, line, owner, spec) in enumerate(manifest)}
current = os.environ.get("BR_GUARD_TEST", "*")
seen = set()
retained = set()
missing = object()
canonical_isinstance = builtins.isinstance
plain_class = vars(object)["__class__"]


def start():
    seen.clear()
    retained.clear()
    (ROOT / (str(os.getpid()) + ".start")).write_text(current)


def save():
    (ROOT / (str(os.getpid()) + ".end")).write_text(json.dumps([sorted(seen), sorted(retained)]))


def exit_process(status):
    try:
        save()
    finally:
        original_exit(status)


def ordinary_negative(value, target):
    cls = type(value)
    if type(cls) is not type or type(target) is not type:
        return None
    mro = type.__getattribute__(cls, "__mro__")
    if any(base is target for base in mro):
        return True
    # On a negative subtype test CPython can still consult value.__class__.
    # Do not call user getters, descriptors or a custom metaclass while inspecting them.
    if any(type(base) is not type for base in mro):
        return None
    getattribute = descriptor = missing
    for base in mro:
        namespace = type.__getattribute__(base, "__dict__")
        if "__getattr__" in namespace:
            return None
        if getattribute is missing:
            getattribute = namespace.get("__getattribute__", missing)
        if descriptor is missing:
            descriptor = namespace.get("__class__", missing)
    if getattribute is object.__getattribute__ and descriptor is plain_class:
        return False
    return None


def line_event(code, line):
    site = sites.get((code.co_filename, line))
    if site is None or code.co_name != site[1]:
        return sys.monitoring.DISABLE
    number, _, (kind, name, module, attr, negate) = site
    event = (current, number)
    if event in retained:
        return
    seen.add(event)
    frame = sys._getframe(1)
    if frame.f_code is not code:
        retained.add(event)
        return
    local = frame.f_locals
    value = local.get(name, missing)
    result = None
    if value is not missing and (name in code.co_varnames or name in code.co_cellvars or name in code.co_freevars):
        if kind == "none":
            result = value is None
        elif (module not in code.co_varnames and module not in code.co_freevars and module not in code.co_cellvars
              and "isinstance" not in code.co_varnames and "isinstance" not in code.co_freevars
              and "isinstance" not in code.co_cellvars
              and frame.f_globals.get("isinstance", frame.f_builtins.get("isinstance")) is canonical_isinstance):
            namespace = frame.f_globals.get(module)
            if type(namespace) is types.ModuleType:
                target = vars(namespace).get(attr, missing)
                result = ordinary_negative(value, target)
    if result is None or result != negate:
        retained.add(event)


start()
os.register_at_fork(after_in_child=start)
original_exit = os._exit
os._exit = exit_process
atexit.register(save)
sys.monitoring.use_tool_id(4, "pristine-deleted-guard-observations")
sys.monitoring.register_callback(4, sys.monitoring.events.LINE, line_event)
sys.monitoring.set_events(4, sys.monitoring.events.LINE)
