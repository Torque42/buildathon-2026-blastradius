"""Record SQLGlot lines executed in a real pytest collection process."""
import atexit
import gzip
import marshal
import os
import sys
from pathlib import Path

active = ""
spans = {}
if "BR_COLLECT_FILE" in os.environ:
    prefix = str(Path(os.environ["BR_COLLECT_PIN"]).resolve()) + "/"
    monitor = sys.monitoring
    slot = 4
    def frame(code,offset):
        if code.co_filename.startswith(prefix+"sqlglot/"):
            monitor.set_local_events(slot,code,monitor.events.LINE)
        return monitor.DISABLE
    def line(code,n):
        paths = spans.setdefault(active,{})
        path = code.co_filename[len(prefix):]
        paths[path] = paths.get(path,0) | (1<<n)
        return monitor.DISABLE
    monitor.use_tool_id(slot,"collection-coverage")
    monitor.register_callback(slot,monitor.events.PY_START,frame)
    monitor.register_callback(slot,monitor.events.LINE,line)
    monitor.set_events(slot,monitor.events.PY_START)
    @atexit.register
    def finish():
        Path(os.environ["BR_COLLECT_FILE"]).write_bytes(gzip.compress(marshal.dumps(spans),2,mtime=0))
