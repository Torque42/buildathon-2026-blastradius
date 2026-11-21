"""Measure cold test-file collection and the ordinary shared pytest collection order."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import marshal
import os
import subprocess
import sys
import tempfile
from pathlib import Path

tools=Path(__file__).resolve().parent
pin,manifest,dest=map(lambda p:Path(p).resolve(),sys.argv[1:])
tests=json.loads(manifest.read_bytes())["tests"]
files=sorted({t["file"] for t in tests})
def merge(out,data):
    for path,mask in data.items():out[path]=out.get(path,0)|mask
def run(name):
    with tempfile.TemporaryDirectory() as tmp:
        out=Path(tmp)/"trace"
        env=dict(os.environ,BR_COLLECT_FILE=str(out),BR_COLLECT_PIN=str(pin),SKIP_INTEGRATION="0",
                 PYTHON_CPU_COUNT="2",OPENBLAS_NUM_THREADS="1",PYTHONDONTWRITEBYTECODE="1",
                 PYTHONPATH=os.pathsep.join(map(str,[tools/"collection_trace",pin])))
        proc=subprocess.run([sys.executable,"-m","pytest","--collect-only","-q","-p","no:cacheprovider","-p","collect_plugin",name or "tests"],cwd=pin,env=env,capture_output=True,text=True)
        if proc.returncode or not out.is_file():
            raise RuntimeError((name,proc.returncode,proc.stdout[-700:],proc.stderr[-500:]))
        discovered={line.split("[",1)[0] for line in proc.stdout.splitlines() if line.startswith("tests/") and "::" in line}
        expected={t["id"] for t in tests if not name or t["file"]==name}
        if not expected <= discovered:raise ValueError((name,len(expected-discovered)))
        return name,marshal.loads(gzip.decompress(out.read_bytes()))
all_files={}
shared={}
with ThreadPoolExecutor(max_workers=3) as pool:
    for name,groups in pool.map(run,["",*files]):
        if not name:
            shared=groups
        else:
            overall={}
            for row in groups.values():merge(overall,row)
            all_files[name]=overall
outside={}
for name,paths in shared.items():
    if name in all_files:merge(all_files[name],paths)
    else:merge(outside,paths)
dest.write_bytes(gzip.compress(marshal.dumps((sorted(t["id"] for t in tests),outside,all_files)),3,mtime=0))
print(len(all_files),len(outside),dest.stat().st_size)
