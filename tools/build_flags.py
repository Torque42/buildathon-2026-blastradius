"""Build callback coverage from the unpatched project, with isolated process-pool tests."""
import gzip
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

base=Path(__file__).resolve().parent
repo=base.parent
source,manifest=map(lambda p:Path(p).resolve(),sys.argv[1:])
pool=json.loads(gzip.decompress((repo/"maps/pool.json.gz").read_bytes()))["tests"]
expected={t["id"] for t in json.loads(manifest.read_bytes())["tests"]}
env=dict(os.environ,PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],PYTHONPATH=os.pathsep.join(map(str,[base/"flag_trace",source])),SKIP_INTEGRATION="0",PYTHON_CPU_COUNT="2",OPENBLAS_NUM_THREADS="1")
readers={}; sites=set();passed=set()
def run(item):
    with tempfile.TemporaryDirectory(prefix="br-callbacks-") as tmp:
        params=dict(env,BR_FLAG_DIR=tmp)
        if item:params["BR_FLAG_TEST"]=item
        else:params["BR_EXCLUDE"]=json.dumps(pool)
        cmd=[sys.executable,"-m","pytest","-q","--tb=short","-p","no:cacheprovider","-p","flag_plugin",item or "tests"]
        result=subprocess.run(cmd,cwd=source,env=params,capture_output=True,text=True)
        if result.returncode:raise RuntimeError(result.stdout[-5000:]+result.stderr[-1500:])
        paths=list(Path(tmp).glob("*.json.gz"))
        if item and len(paths)<2:raise RuntimeError("Missing subprocess trace: "+item)
        return [json.loads(gzip.decompress(p.read_bytes())) for p in paths],json.loads(Path(tmp,"passed.json").read_text())
def add(result):
    traces,done=result
    passed.update(n.split("[",1)[0] for n in done)
    for covered,events in traces:
        sites.update(covered)
        for site,ids in events.items():readers.setdefault(site,set()).update(n.split("[",1)[0] for n in ids)
with ThreadPoolExecutor(max_workers=2) as workers:
    for result in workers.map(run,[None,*pool]):add(result)
if expected-passed:raise ValueError(sorted(expected-passed))
payload={"catalog":sorted(expected),"sites":sorted(sites),"readers":{s:sorted(ids) for s,ids in readers.items()}}
(repo/"maps/flags.json.gz").write_bytes(gzip.compress(json.dumps(payload,separators=(",",":")).encode(),mtime=0))
print(len(sites),len(readers),len(passed))
