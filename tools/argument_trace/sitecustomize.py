"""Observe reads of literal call arguments and defaults without replacing Python functions."""
import atexit
import dis
import gzip
import marshal
import os
import sys
import threading
import types
from functools import lru_cache
from pathlib import Path

if "BR_ARG_PIN" in os.environ:
    from argument_sites import locate
    pin = Path(os.environ["BR_ARG_PIN"]).resolve()
    prefix = str(pin) + "/sqlglot/"
    active = os.environ.get("BR_FLAG_TEST", "")
    monitor = sys.monitoring
    tool = 4
    events = monitor.events
    programs = {}
    calling = {}
    tracking = {}
    imports = set()
    calls_seen = {}
    accepted = {}
    registered = set()
    read_by = {}
    # Introspective functions can observe a formal without a LOAD_FAST.
    reflective = {"locals", "vars", "eval", "exec", "inspect", "f_locals", "f_back",
                  "currentframe", "_getframe", "_current_frames", "getargvalues", "getouterframes"}

    @lru_cache(None)
    def source_sites(path):
        return locate(path, (pin / path).read_bytes())

    def context():
        return "*" if imports or not active else active.split("[", 1)[0]

    def retain(site, ctx):
        read_by.setdefault(site, set()).add(ctx)

    def describe(code):
        path = code.co_filename[len(str(pin))+1:]
        calls, defaults, _ = source_sites(path)
        call_at, loads, stores = {}, {}, {}
        previous = None
        for ins in dis.get_instructions(code):
            if ins.opname in ("CALL", "CALL_KW", "CALL_FUNCTION_EX"):
                p = ins.positions
                site = calls.get((p.lineno, p.col_offset, p.end_lineno, p.end_col_offset))
                names = ()
                # CPython 3.13 CALL_KW consumes this constant tuple immediately
                # after the argument values. Their values can be arbitrary.
                if (ins.opname == "CALL_KW" and previous and previous.opname == "LOAD_CONST"
                        and type(previous.argval) is tuple and all(type(k) is str for k in previous.argval)):
                    names = previous.argval
                if site or names:
                    call_at[ins.offset] = (site or {}, names)
                    if site:
                        registered.update(site.values())
            if "FAST" in ins.opname:
                names = ins.argval if isinstance(ins.argval, tuple) else (ins.argval,)
                if ins.opname == "STORE_FAST_LOAD_FAST":
                    # Record the read even when the two names are equal.
                    loads[ins.offset] = (names[-1],)
                    stores[ins.offset] = (names[0],)
                elif "LOAD" in ins.opname:
                    loads[ins.offset] = names
                elif "STORE" in ins.opname or "DELETE" in ins.opname:
                    stores[ins.offset] = names
            previous = ins
        params = defaults.get((code.co_firstlineno, code.co_name), {})
        registered.update(site for site, _ in params.values())
        opaque = bool(set(code.co_names) & reflective or code.co_flags & (0x20|0x80|0x200))
        return call_at, loads, stores, params, opaque

    def call(code, offset, fn, arg0):
        entry = programs[code][0].get(offset)
        if entry is None:
            return monitor.DISABLE
        sites, keywords = entry
        caller = sys._getframe(1)
        calling.pop(id(caller), None)
        ctx = context()
        if type(fn) is types.MethodType:
            fn = fn.__func__
        if type(fn) is not types.FunctionType or not fn.__code__.co_filename.startswith(prefix):
            for key in sites.values():
                retain(key, ctx)
            return
        target = fn.__code__
        calling[id(caller)] = (target, offset, sites, ctx, keywords, code)
        for key in sites.values():
            ref = (key, ctx)
            calls_seen[ref] = calls_seen.get(ref, 0) + 1

    def enter(code, offset):
        if not code.co_filename.startswith(prefix):
            return monitor.DISABLE
        frame = sys._getframe(1)
        if code not in programs:
            programs[code] = describe(code)
        call_at, _, _, defaults, opaque = programs[code]
        callbacks = events.CALL if call_at else 0
        if code.co_name == "<module>":
            imports.add(id(frame))
            callbacks |= events.PY_RETURN
        owner = frame.f_back
        origin = calling.get(id(owner))
        explicit = {}
        supplied = ()
        ctx = context()
        if origin and origin[0] is code and origin[1] == owner.f_lasti and origin[5] is owner.f_code:
            _, _, explicit, ctx, supplied, _ = calling.pop(id(owner))
        keyword_formals = code.co_varnames[code.co_posonlyargcount:code.co_argcount + code.co_kwonlyargcount]
        watch = {}
        for name, key in explicit.items():
            ref = (key, ctx)
            accepted[ref] = accepted.get(ref, 0) + 1
            if (opaque or name in code.co_cellvars or
                    name not in code.co_varnames[code.co_posonlyargcount:code.co_argcount+code.co_kwonlyargcount]):
                retain(key, ctx)
            else:
                watch.setdefault(name, []).append((key, ctx))
        for name, (key, value) in defaults.items():
            # A name matching a positional-only formal might have been accepted
            # separately by **kwargs. Its default can still be observed.
            if name in keyword_formals and (name in supplied or name in explicit):
                continue
            if opaque or name in code.co_cellvars:
                retain(key, ctx)
                continue
            current = frame.f_locals.get(name)
            # A value with a different exact type or value necessarily came from the caller.
            if type(current) is type(value) and current == value:
                watch.setdefault(name, []).append((key, ctx))
        if watch:
            tracking[id(frame)] = watch
        if watch or id(code) in armed:
            armed.add(id(code))
            callbacks |= events.INSTRUCTION | events.PY_RETURN
        if callbacks and monitor.get_local_events(tool, code) != callbacks:
            monitor.set_local_events(tool, code, callbacks)

    def instruction(code, offset):
        _, reads, writes, _, _ = programs[code]
        load = reads.get(offset, ())
        store = writes.get(offset, ())
        if not load and not store:
            return monitor.DISABLE
        watch = tracking.get(id(sys._getframe(1)))
        if watch:
            for name in load:
                for key, ctx in watch.pop(name, ()):
                    retain(key, ctx)
            for name in store:
                watch.pop(name, None)

    def leave(code, offset, result):
        token = id(sys._getframe(1))
        tracking.pop(token, None)
        calling.pop(token, None)
        imports.discard(token)

    def write():
        for (site, ctx), count in calls_seen.items():
            if accepted.get((site, ctx), 0) != count:
                retain(site, ctx)
        out = Path(os.environ["BR_FLAG_DIR"]) / (str(os.getpid())+".bin.gz")
        out.write_bytes(gzip.compress(marshal.dumps((registered, read_by)), compresslevel=2, mtime=0))

    armed = set()
    monitor.use_tool_id(tool, "argument-liveness")
    for event, callback in [(events.PY_START, enter), (events.INSTRUCTION, instruction),
                            (events.CALL, call), (events.PY_RETURN, leave), (events.PY_UNWIND, leave)]:
        monitor.register_callback(tool, event, callback)
    monitor.set_events(tool, events.PY_START | events.PY_UNWIND)
    atexit.register(write)
