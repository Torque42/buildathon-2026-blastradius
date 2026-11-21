import atexit
import json
import os
import sys

root = os.environ["PIN_ROOT"] + "/"
import keytrace
keytrace.install()
seen = {}
reads = set()
entered = {}
inspected = {}

def start(code, offset):
    name = code.co_filename
    if name.startswith(root + "sqlglot/"):
        entered.setdefault(name[len(root):], set()).add(code.co_qualname)
    return sys.monitoring.DISABLE

def record(code, line):
    name = code.co_filename
    if name.startswith(root + "sqlglot/"):
        seen.setdefault(name[len(root):], set()).add(line)
    return sys.monitoring.DISABLE

def observe(event, args):
    if event == "object.__getattr__" and args[1] in {"__code__", "f_code"}:
        if args[1] == "__code__":
            import types
            if type(args[0]) is types.FunctionType and args[0].__module__ and args[0].__module__.startswith("sqlglot"):
                module = sys.modules.get(args[0].__module__)
                filename = getattr(module, "__file__", "") or ""
                if filename.startswith(root):
                    inspected.setdefault(filename[len(root):], set()).add(args[0].__qualname__)
    if event == "open" and isinstance(args[0], str):
        path = os.path.abspath(args[0])
        if path.startswith(root + "sqlglot/") and path.endswith(".py"):
            frame = sys._getframe(1)
            while frame and frame.f_code.co_filename.startswith("<frozen importlib"):
                frame = frame.f_back
            if frame and frame.f_code.co_filename == keytrace.__file__ and frame.f_code.co_name == "get_code":
                return
            reads.add(path[len(root):])

def save():
    with open(os.path.join(os.environ["PIN_OUT"], str(os.getpid()) + ".json"), "w") as stream:
        keys = {field: sorted({key if key is not None else "*" for values in contexts.values() for key in values}) for field, contexts in keytrace.value_reads.items() if field.endswith(":NUMERIC_ESCAPES")}
        json.dump([sorted(reads), {name: sorted(lines) for name, lines in seen.items()}, {name: sorted(names) for name, names in entered.items()}, keys, {name: sorted(names) for name,names in inspected.items()}], stream)

atexit.register(save)
sys.addaudithook(observe)
sys.monitoring.use_tool_id(4, "pin-reach")
sys.monitoring.register_callback(4, sys.monitoring.events.LINE, record)
sys.monitoring.register_callback(4, sys.monitoring.events.PY_START, start)
sys.monitoring.set_events(4, sys.monitoring.events.LINE | sys.monitoring.events.PY_START)
