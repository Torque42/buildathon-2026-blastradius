"""Check patched package initialization when a source change reaches test collection."""
import importlib
import marshal
import os
import signal
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT=Path(__file__).resolve().parent
PIN=ROOT/"cache/pinned"
CACHE=ROOT/"cache/collection-inputs"

def modules():
    return [p.relative_to(PIN).with_suffix("").as_posix().replace("/",".")
            for p in sorted((PIN/"tests").rglob("test_*.py"))]

def observe(names):
    import unittest
    sys.path.insert(0,str(PIN))
    prefix = str(PIN) + "/sqlglot/"
    monitor = sys.monitoring
    slot = 4
    seen = {}
    def record(code, line):
        if code.co_filename.startswith(prefix):
            path = "sqlglot/" + code.co_filename[len(prefix):]
            seen[path] = seen.get(path, 0) | (1 << line)
        return monitor.DISABLE
    monitor.use_tool_id(slot, "collection")
    monitor.register_callback(slot, monitor.events.LINE, record)
    monitor.set_events(slot, monitor.events.LINE)
    ordered = {}
    try:
        for name in names:
            seen.clear()
            monitor.restart_events()
            importlib.import_module(name)
            ordered[name] = seen.copy()
    finally:
        monitor.set_events(slot, 0)
    return ordered

def build():
    from concurrent.futures import ThreadPoolExecutor
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        def available(name):
            output = Path(tmp)/name
            run = subprocess.run([sys.executable,"-I","-S",str(Path(__file__).resolve()),
                                  "check",name,str(output)], stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return (name, marshal.loads(output.read_bytes())) if run.returncode == 0 else None
        with ThreadPoolExecutor(max_workers=3) as pool:
            separate = dict(row for row in pool.map(available, modules()) if row)
    if not separate:
        raise RuntimeError("No pristine collection modules")
    # Preserve execution seen in the usual shared module cache as well.
    ordered = observe(separate)
    seen = {}
    for name, files in separate.items():
        for path, mask in ordered[name].items():
            files[path] = files.get(path, 0) | mask
        for path, mask in files.items():
            seen[path] = seen.get(path, 0) | mask
    CACHE.write_bytes(marshal.dumps((list(separate),seen,separate)))

@lru_cache(1)
def inputs():
    return marshal.loads(CACHE.read_bytes())

def sources(selector,text):
    patches=selector.parse_patch(text)
    if not patches or len(patches)>6:return
    code=[]
    for patch in patches:
        p=patch["old"]
        if not p or p!=patch["new"]:return
        if p.startswith("tests/fixtures/"):continue
        if not p.startswith("sqlglot/") or p not in selector.SRC or not p.endswith(".py"):return
        old=selector.SRC[p]
        new,removed,added=selector.apply(old,patch["hunks"])
        if not (removed or added):continue
        code.append((p,new))
    return code

def failed(selector,text,selected):
    edits=sources(selector,text)
    if not edits:return False
    evidence=inputs()
    checks,seen=evidence[:2]
    per_module=evidence[2] if len(evidence)>2 else None
    # A patch confined to an existing expression cannot affect a clean import
    # before that expression first executes. Top-level declarations are checked.
    from replay_edit import possible_edit,Overlay
    simple=possible_edit(selector,text)
    if simple:
        path,_,mask=simple
        if not mask & seen.get(path,0):return False
    if not any(path in seen for path,_ in edits):return False
    if per_module is not None:
        masks = {path:None for path,_ in edits}
        if simple:
            masks[simple[0]] = simple[2]
        else:
            from compound import pieces
            for piece in pieces(selector,text):
                part = possible_edit(selector,piece)
                if part is not None and part[0] in masks:
                    masks[part[0]] = part[2]
        def affected(name):
            files = per_module[name]
            return any(path in files and (mask is None or bool(files[path] & mask))
                       for path,mask in masks.items())
        checks = [name for name in checks if affected(name)]
        if not checks:
            return False
    if selected and selector.worker_cost(selector.split(selected))>=selector.full_schedule()[0]:return False
    if any(n=="sqlglot" or n.startswith(("sqlglot.","tests.")) for n in sys.modules):return True
    import unittest
    try:pid=os.fork()
    except OSError:return True
    if pid==0:
        try:
            with open(os.devnull,"wb") as null:
                os.dup2(null.fileno(),1);os.dup2(null.fileno(),2)
            signal.signal(signal.SIGALRM,signal.SIG_DFL)
            signal.setitimer(signal.ITIMER_REAL,2.8)
            sys.path.insert(0,str(PIN))
            for path,new in edits:sys.meta_path.insert(0,Overlay(path,new,PIN))
            for name in checks:importlib.import_module(name)
        except BaseException:
            os._exit(1)
        os._exit(0)
    _,status=os.waitpid(pid,0)
    return status!=0

@lru_cache(1)
def pytest_collection():
    import gzip
    catalog, outside, files = marshal.loads(gzip.decompress((ROOT/"maps/pytest-collection.gz").read_bytes()))
    return set(catalog), outside, files


def include_failed(selector, text, selected):
    """Preserve every module that could encounter the first altered expression during collection."""
    from replay_edit import possible_edit
    edit = possible_edit(selector, text)
    if edit is None:
        return set(selector.ALL)
    path, _, mask = edit
    import import_impact
    # A failed package initializer can affect later imports through shared globals.
    # Only use file-level attribution when no package initialization reached this edit.
    for line in import_impact.profile()[0].get(path, {}):
        if mask & (1 << line):
            return set(selector.ALL)
    catalog, outside, files = pytest_collection()
    if catalog != set(selector.NUM) or set(files) != set(selector.BY_FILE) or outside.get(path, 0) & mask:
        return set(selector.ALL)
    affected = set()
    for name, coverage in files.items():
        if mask & coverage.get(path,0):
            affected.update(selector.BY_FILE[name])
            affected.update(selector.dependent_test_numbers(name))
    # An observed import failure with no matching complete-suite evidence is unexplained.
    return selected | affected if affected else set(selector.ALL)


if __name__=="__main__":
    if sys.argv[1]=="check":
        name=sys.argv[2]
        Path(sys.argv[3]).write_bytes(marshal.dumps(observe([name])[name]))
    else:build()
