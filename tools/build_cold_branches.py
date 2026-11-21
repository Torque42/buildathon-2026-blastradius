"""Collect independent test-file profiles. Incomplete files remain unknown."""
import gzip,json,marshal,os,subprocess,sys,tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
tools=Path(__file__).resolve().parent
root,manifest,dest=[Path(p).resolve() for p in sys.argv[1:4]]
tests=json.loads(manifest.read_bytes())["tests"]
names=sorted(t["id"] for t in tests)
pool=set(json.load(gzip.open(tools.parent/"maps/pool.json.gz"))["tests"])
bits={n:1<<i for i,n in enumerate(names)}
files={}
for t in tests:files.setdefault(t["file"],set()).add(t["id"])
heavy={n for n in names if n.endswith("::test_max_nodes") or n.endswith("::test_generator_memory")}
jobs=[(f,ids-(pool|heavy)) for f,ids in files.items()]
jobs.extend((node,{node}) for node in sorted(pool|heavy))
jobs=[x for x in jobs if x[1]]
def collect(job):
    target,expected=job;single="::" in target
    with tempfile.TemporaryDirectory() as t:
        scope=0
        for n in expected:scope|=bits[n]
        env=dict(os.environ,BR_FLAG_DIR=t,BR_FLAG_TEST=target if single else "scope:"+target,BR_EXCLUDE=json.dumps([] if single else sorted(heavy|pool)),PYTHONPATH=os.pathsep.join(map(str,[tools/"branch_trace",tools.parent,root])),PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],SKIP_INTEGRATION="0",PYTHON_CPU_COUNT="2",OPENBLAS_NUM_THREADS="1",PYTHONDONTWRITEBYTECODE="1")
        try:run=subprocess.run([sys.executable,"-m","pytest","-q","--tb=short","-p","no:cacheprovider","-p","flag_plugin",target],cwd=root,env=env,capture_output=True,text=True,timeout=190 if single else 250)
        except subprocess.TimeoutExpired:return target,0,{}
        log=Path(t,"passed.json");paths=list(Path(t).glob("*.rec"))
        if run.returncode or "Error in sitecustomize" in run.stderr or not log.is_file() or not paths:return target,0,{}
        if target in pool and len(paths)<2:return target,0,{}
        done={n.split("[",1)[0] for n in json.loads(log.read_bytes())}
        if expected-done:return target,0,{}
        merged={}
        for path in paths:
            for site,contexts in marshal.loads(gzip.decompress(path.read_bytes())).items():
                row=merged.setdefault(site,{})
                for ctx,states in contexts.items():
                    group=bits.get(ctx,scope)
                    for state in states:row[state]=row.get(state,0)|group
        return target,scope,merged
known=0;records={}
with ThreadPoolExecutor(max_workers=3) as workers:
    for target,scope,rows in workers.map(collect,jobs):
        known|=scope
        for site,states in rows.items():
            out=records.setdefault(site,{})
            for state,mask in states.items():out[state]=out.get(state,0)|mask
        print(target,scope.bit_count(),flush=True)
unknown=((1<<len(names))-1)^known
for row in records.values():
    if unknown:row[None]=row.get(None,0)|unknown
dest.write_bytes(gzip.compress(marshal.dumps((names,records)),mtime=0))
print("certified",known.bit_count(),"of",len(names),"tests",len(records),"sites")
