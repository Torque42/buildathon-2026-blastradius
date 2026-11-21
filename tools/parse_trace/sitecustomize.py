"""Partition pristine parser coverage by complete Dialect.parse calls."""
import atexit
import functools
import gzip
import importlib.abc
import importlib.machinery
import logging
import marshal
import os
import sys
import threading
from pathlib import Path

if "BR_PARSE_DIR" in os.environ:
    from parse_observations import parse_key, result_digest
    base = Path(os.environ["BR_PARSE_PIN"]).resolve()
    prefix = str(base) + "/"
    monitor = sys.monitoring
    slot = 4
    outside = {}
    spans = outside
    calls = {}
    installed = False
    in_call = False
    observed_log = False
    owner = threading.get_ident()
    cross_thread = False

    def supported(path):
        return (path in ("sqlglot/parser.py", "sqlglot/tokens.py", "sqlglot/tokenizer_core.py")
                or path.startswith("sqlglot/parsers/"))

    def enter(code, offset):
        file = code.co_filename
        if file.startswith(prefix) and supported(file[len(prefix):]):
            monitor.set_local_events(slot, code, monitor.events.LINE)
        return monitor.DISABLE

    def line(code, n):
        global cross_thread
        if threading.get_ident() != owner:
            cross_thread = True
        path = code.co_filename[len(prefix):]
        spans[path] = spans.get(path, 0) | (1 << n)
        return monitor.DISABLE

    class LogWatcher(logging.Handler):
        def emit(self, record):
            global observed_log
            if in_call:
                observed_log = True

    def merge(target, source):
        for path, mask in source.items():
            target[path] = target.get(path, 0) | mask

    def wrap(original):
        @functools.wraps(original)
        def invoke(self, sql, **opts):
            global spans, in_call, observed_log
            if in_call:
                return original(self, sql, **opts)
            key = parse_key(self, sql, opts)
            if key is None:
                return original(self, sql, **opts)
            in_call = True
            observed_log = False
            spans = {}
            monitor.restart_events()
            digest = None
            try:
                value = original(self, sql, **opts)
                if not observed_log:
                    try:
                        digest = result_digest(value)
                    except Exception:
                        pass
                return value
            finally:
                gathered, spans = spans, outside
                in_call = False
                monitor.restart_events()
                if digest is None:
                    merge(outside, gathered)
                else:
                    merge(calls.setdefault((key,digest), {}), gathered)
        return invoke

    class Loader(importlib.machinery.SourceFileLoader):
        def exec_module(self, module):
            global installed
            super().exec_module(module)
            module.Dialect.parse = wrap(module.Dialect.parse)
            installed = True

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "sqlglot.dialects.dialect":
                spec = importlib.machinery.PathFinder.find_spec(fullname, path)
                if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                    spec.loader = Loader(fullname, spec.origin)
                return spec

    def save():
        dest = Path(os.environ["BR_PARSE_DIR"]) / (str(os.getpid())+".gz")
        dest.write_bytes(gzip.compress(marshal.dumps((installed and not cross_thread, outside, calls)), 2,mtime=0))

    logging.getLogger().addHandler(LogWatcher())
    monitor.use_tool_id(slot, "parse-boundaries")
    monitor.register_callback(slot, monitor.events.PY_START, enter)
    monitor.register_callback(slot, monitor.events.LINE, line)
    monitor.set_events(slot, monitor.events.PY_START)
    sys.meta_path.insert(0, Finder())
    atexit.register(save)
