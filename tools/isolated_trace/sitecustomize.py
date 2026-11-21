import atexit
import json
import os
import sys

root = os.environ["PIN_ROOT"] + "/"
seen = {}
reads = set()

def record(code, line):
    name = code.co_filename
    if name.startswith(root + "sqlglot/"):
        seen.setdefault(name[len(root):], set()).add(line)
    return sys.monitoring.DISABLE

def observe(event, args):
    if event == "open" and isinstance(args[0], str):
        path = os.path.abspath(args[0])
        if path.startswith(root + "sqlglot/") and path.endswith(".py"):
            reads.add(path[len(root):])

def save():
    with open(os.path.join(os.environ["PIN_OUT"], str(os.getpid()) + ".json"), "w") as stream:
        json.dump([sorted(reads), {name: sorted(lines) for name, lines in seen.items()}], stream)

atexit.register(save)
sys.addaudithook(observe)
sys.monitoring.use_tool_id(4, "pin-reach")
sys.monitoring.register_callback(4, sys.monitoring.events.LINE, record)
sys.monitoring.set_events(4, sys.monitoring.events.LINE)
