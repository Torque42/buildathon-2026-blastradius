"""Compare the complete affected SQL parse calls of observed tests against pristine results."""
from functools import lru_cache
import importlib
import logging
import lzma
import marshal
import os
from pathlib import Path
import signal
import sys
import warnings

ROOT = Path(__file__).resolve().parent

@lru_cache(1)
def profiles():
    cached = ROOT/"cache/parse-boundaries"
    if cached.is_file():
        return marshal.loads(cached.read_bytes())
    return marshal.loads(lzma.decompress((ROOT/"maps/parse-boundaries.xz").read_bytes()))


def bits(mask):
    while mask:
        bit = mask & -mask
        mask -= bit
        yield bit.bit_length()-1


def refine(selector, text, selected):
    if not selected:
        return selected
    from replay_edit import possible_edit, preserve_scope
    edit = possible_edit(selector, text)
    if edit is None:
        return selected
    path, source, mask = edit
    if not (path in {"sqlglot/parser.py","sqlglot/tokenizer_core.py","sqlglot/tokens.py"}
            or path.startswith("sqlglot/parsers/")):
        return selected
    import import_impact, collection_guard
    if (mask & collection_guard.inputs()[1].get(path,0)
            or any(n in import_impact.profile()[0].get(path,{}) for n in bits(mask))):
        return selected
    protected = preserve_scope(selector,path,mask,selected)
    protected |= selector.tests_of(selector.M["opens"].get(path))
    protected.update(n for n,(files,_) in selector.EXTRA.items() if path in files)
    calls, lines, tests = profiles()
    affected = 0
    for line in bits(mask):
        affected |= lines.get(path,{}).get(line,0)
    jobs = []
    free = set()
    for name, (outside, own_calls) in tests.items():
        n = selector.NUM.get(name)
        if n not in selected or n in protected or outside.get(path,0) & mask:
            continue
        required = own_calls & affected
        if not required:
            free.add(n)
        else:
            indexes = list(bits(required))
            # Batches of tiny statements often have thousands of callers. Never
            # spend the full CPU allowance on work whose worker saving is small.
            length = sum(len(calls[i][0][1]) + 128 for i in indexes)
            jobs.append((length/max(selector.DUR[n],1), n, indexes))
    current = selected - free
    if not jobs:
        return current
    possible = (selector.worker_cost(selector.split(current)) -
                selector.worker_cost(selector.split(current-{n for _,n,_ in jobs})))/1000
    if possible < 0.45 or any(k=="sqlglot" or k.startswith("sqlglot.") for k in sys.modules):
        return current
    budget = min(3.2, possible * 0.46)
    return current - verify(edit, sorted(jobs), calls, budget)


def verify(edit, jobs, calls, budget):
    try:
        rd, wr = os.pipe()
        try:
            pid = os.fork()
        except OSError:
            os.close(rd)
            os.close(wr)
            return set()
    except OSError:
        return set()
    if pid == 0:
        try:
            os.close(rd)
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(),1)
                os.dup2(sink.fileno(),2)
            signal.signal(signal.SIGPROF,signal.SIG_DFL)
            signal.signal(signal.SIGALRM,signal.SIG_DFL)
            signal.setitimer(signal.ITIMER_PROF,budget)
            signal.setitimer(signal.ITIMER_REAL,max(1.2,2*budget+0.5))
            run(edit,jobs,calls,lambda n:os.write(wr,(str(n)+"\n").encode()))
        finally:
            os._exit(0)
    os.close(wr)
    try:
        with os.fdopen(rd,"rb") as stream:
            passed = {int(line) for line in stream}
    finally:
        os.waitpid(pid,0)
    return passed


def run(edit, jobs, calls, report):
    import unittest
    from replay_edit import Overlay
    from parse_observations import result_digest
    pin = ROOT/"cache/pinned"
    sys.path.insert(0,str(pin))
    path,source,_ = edit
    sys.meta_path.insert(0,Overlay(path,source,pin))
    from sqlglot import Dialect
    class Watch(logging.Handler):
        def emit(self, record):
            self.observed = True
    watch = Watch()
    logging.getLogger().addHandler(watch)
    # Fresh dialect instances are reconstructed from the pinned builtin class.
    # A warning, changed diagnostic, exception, or incomplete object is uncertain.
    checked = {}
    for _,n,ids in jobs:
        for i in ids:
            if i not in checked:
                (kind,sql,opts), expected = calls[i]
                try:
                    cls = getattr(importlib.import_module(kind[0]),kind[1])
                    if not issubclass(cls,Dialect):
                        checked[i] = False
                        break
                    dialect = cls()
                    watch.observed = False
                    with warnings.catch_warnings(record=True) as caught:
                        warnings.simplefilter("always")
                        result = dialect.parse(sql,**dict(opts))
                        digest = result_digest(result)
                    checked[i] = not caught and not watch.observed and digest == expected
                except Exception:
                    checked[i] = False
            if not checked[i]:
                break
        else:
            report(n)
