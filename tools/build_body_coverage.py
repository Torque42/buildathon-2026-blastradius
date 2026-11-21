"""Collect individually isolated, pristine test executions and compact repeated bitmaps."""
import gzip
import json
import marshal
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

tools = Path(__file__).resolve().parent
root, catalog, scratch = (Path(x).resolve() for x in sys.argv[1:4])
tests = json.loads(catalog.read_bytes())["tests"]
pool = set(json.load(gzip.open(tools.parent/"maps/pool.json.gz"))["tests"])
env = dict(os.environ, PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],
           BR_PIN=str(root), PYTHONPATH=os.pathsep.join(map(str,(tools/"body_trace",root))),
           SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
scratch.mkdir(exist_ok=True,parents=True)

def collect(test):
    output = scratch/(str(test["number"])+".gz")
    if output.is_file():return
    with tempfile.TemporaryDirectory() as tmp:
        p = subprocess.run([sys.executable,"-m","pytest","-q","-p","no:cacheprovider",test["id"]],
                           cwd=root,env=dict(env, BR_BODY_OUT=tmp),capture_output=True)
        logs=list(Path(tmp).glob("*.record"))
        if p.returncode or b" passed" not in p.stdout or not logs or test["id"] in pool and len(logs)<2:
            print("untraced",test["id"],flush=True);return
        lines={};owners={}
        for log in logs:
            a,b=marshal.loads(log.read_bytes())
            for group,src in ((lines,a),(owners,b)):
                for path,nums in src.items():group.setdefault(path,set()).update(nums)
        if "sqlglot/__init__.py" not in lines:return
        def bits(m):
            return {path:sum(1<<n for n in nums) for path,nums in m.items()}
        output.write_bytes(gzip.compress(marshal.dumps((test["id"],bits(lines),bits(owners))),compresslevel=1,mtime=0))

subset=tests
if len(sys.argv)>4:
    subset=sorted(tests,key=lambda t:(-t["seconds"],t["id"]))[:int(sys.argv[4])]
with ThreadPoolExecutor(max_workers=6) as workers:
    list(workers.map(collect,subset))

# A column is a set of tests that reached a specific pinned line or function.
known={};locations={}
for test in tests:
    out=scratch/(str(test["number"])+".gz")
    if not out.is_file():continue
    name,a,b=marshal.loads(gzip.decompress(out.read_bytes()))
    if name!=test["id"]:raise ValueError(name)
    ix=len(known);known[name]=ix
    for path in a:
        pair=locations.setdefault(path,({},{}))
        for slot,mask in zip(pair,(a[path],b[path])):
            slot[mask]=slot.get(mask,0)|(1<<ix)
payload=(known,locations)
(tools.parent/"maps"/"body-coverage.gz").write_bytes(gzip.compress(marshal.dumps(payload),mtime=0))
print("profiled",len(known),"of",len(subset),"size",(tools.parent/"maps"/"body-coverage.gz").stat().st_size,flush=True)
