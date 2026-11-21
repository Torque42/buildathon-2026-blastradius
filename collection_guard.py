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

def build():
    # Discover usable original test modules with exactly the stdlib available
    # in the submitted image. Separate processes keep partial imports isolated.
    import concurrent.futures
    def available(name):
        p=subprocess.run([sys.executable,"-I","-S",str(Path(__file__).resolve()),"check",name],
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return name if p.returncode==0 else None
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        checks=[m for m in pool.map(available,modules()) if m]
    if not checks:raise RuntimeError("No pristine collection modules")
    import unittest
    sys.path.insert(0,str(PIN))
    seen={};prefix=str(PIN)+"/sqlglot/";monitor=sys.monitoring;slot=4
    def record(code,line):
        if code.co_filename.startswith(prefix):
            path="sqlglot/"+code.co_filename[len(prefix):]
            seen[path]=seen.get(path,0)|(1<<line)
        return monitor.DISABLE
    monitor.use_tool_id(slot,"collection")
    monitor.register_callback(slot,monitor.events.LINE,record)
    monitor.set_events(slot,monitor.events.LINE)
    for name in checks:importlib.import_module(name)
    monitor.set_events(slot,0)
    CACHE.write_bytes(marshal.dumps((checks,seen)))

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
    checks,seen=inputs()
    # A patch confined to an existing expression cannot affect a clean import
    # before that expression first executes. Top-level declarations are checked.
    from replay_edit import possible_edit,Overlay
    simple=possible_edit(selector,text)
    if simple:
        path,_,mask=simple
        if not mask & seen.get(path,0):return False
    if not any(path in seen for path,_ in edits):return False
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

if __name__=="__main__":
    if sys.argv[1]=="check":
        import unittest
        sys.path.insert(0,str(PIN))
        importlib.import_module(sys.argv[2])
    else:build()
