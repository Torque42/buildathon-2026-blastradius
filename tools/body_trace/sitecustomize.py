"""Record each entered SQLGlot function and executable line on the pristine pin."""
import atexit
import marshal
import os
import sys
from pathlib import Path

prefix = os.environ["BR_PIN"] + "/sqlglot/"
dest = Path(os.environ["BR_BODY_OUT"])
events = sys.monitoring.events
tool = 4
files = {}
hits = {}
owners = {}


def enter(code, offset):
    filename = code.co_filename
    if filename.startswith(prefix):
        rel = "sqlglot/" + filename[len(prefix):]
        files[code] = rel
        owners.setdefault(rel, set()).add(code.co_firstlineno)
        sys.monitoring.set_local_events(tool, code, events.LINE)
    return sys.monitoring.DISABLE


def line(code, number):
    hits.setdefault(files[code], set()).add(number)
    return sys.monitoring.DISABLE


def save():
    with (dest / (str(os.getpid()) + ".record")).open("wb") as f:
        marshal.dump((hits, owners), f)


atexit.register(save)
sys.monitoring.use_tool_id(tool, "isolated-body")
sys.monitoring.register_callback(tool, events.PY_START, enter)
sys.monitoring.register_callback(tool, events.LINE, line)
sys.monitoring.set_events(tool, events.PY_START)
