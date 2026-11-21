"""Profile the full pristine suite and separately attribute process-pool workers."""
import ast
import gzip
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

tools=Path(__file__).resolve().parent
sys.path.insert(0,str(tools.parent))
from consumer_sites import scan
source,manifest,output=[Path(v).resolve() for v in sys.argv[1:4]]
catalog=sorted(t["id"] for t in json.loads(manifest.read_bytes())["tests"])
pool=json.load(gzip.open(tools.parent/"maps/pool.json.gz"))["tests"]
slots={}
for file in (source/"sqlglot").rglob("*.py"):
    p=file.relative_to(source).as_posix()
    for _,_,_,kind,owner,_,_ in scan(p,ast.parse(file.read_bytes())):
        if kind in ("slot","default"):
            _,cls,attr=owner.split(":")
            fields=slots.setdefault(p,{}).setdefault(cls,[])
            if attr not in fields:fields.append(attr)
with tempfile.TemporaryDirectory() as lab:
    planning=Path(lab)/"plan.json"
    planning.write_text(json.dumps(dict(slots=slots)))
    base=dict(os.environ,BR_CONSUMER_PLAN=str(planning),PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],PYTHONPATH=os.pathsep.join(map(str,[tools/"consumer_trace",source])),PYTHONDONTWRITEBYTECODE="1",SKIP_INTEGRATION="0",PYTHON_CPU_COUNT="2",OPENBLAS_NUM_THREADS="1")
    def run(node):
        with tempfile.TemporaryDirectory(dir=lab) as tmp:
            env=dict(base,BR_FLAG_DIR=tmp,BR_FLAG_TEST=node or "",BR_EXCLUDE=json.dumps([] if node else pool))
            cmd=[sys.executable,"-m","pytest","-q","--tb=short","-p","no:cacheprovider","-p","flag_plugin",node or "tests"]
            result=subprocess.run(cmd,cwd=source,env=env,capture_output=True,text=True,timeout=600)
            if result.returncode:raise RuntimeError(result.stdout[-5000:]+result.stderr[-1000:])
            records=list(Path(tmp).glob("*.json.gz"))
            if node and len(records)<2:raise ValueError("Missing child process",node)
            return [json.loads(gzip.decompress(p.read_bytes())) for p in records],json.loads(Path(tmp,"passed.json").read_bytes())
    checked=set(); sites=set(); reads={}
    with ThreadPoolExecutor(max_workers=2) as workers:
        for logs,passed in workers.map(run,[None,*pool]):
            checked.update(n.split("[",1)[0] for n in passed)
            if not any(row[1] for row in logs):raise ValueError("Missing tokenizer")
            for registered,_,events in logs:
                sites.update(registered)
                for key,ids in events.items():reads.setdefault(key,set()).update(n.split("[",1)[0] for n in ids)
    if set(catalog)-checked:raise ValueError("Incomplete catalog",set(catalog)-checked,checked-set(catalog))
    result={"catalog":catalog,"slots":sorted(sites),"reads":{k:sorted(v) for k,v in reads.items()}}
    output.write_bytes(gzip.compress(json.dumps(result,separators=(",",":")).encode(),mtime=0))
    print("profiled",len(catalog),"tests",len(sites),"slots",len(reads),"distinct reads")
