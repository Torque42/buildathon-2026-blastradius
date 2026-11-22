"""Collect unmodified branch operands, preserving setup and subprocess attribution."""
import gzip,json,marshal,os,subprocess,sys,tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
tools=Path(__file__).resolve().parent
root,manifest,dest=[Path(p).resolve() for p in sys.argv[1:4]]
plugin=sys.argv[4] if len(sys.argv)>4 else "branch_trace"
if plugin not in ("branch_trace","access_trace","export_trace"):raise ValueError(plugin)
names=sorted(t["id"] for t in json.loads(manifest.read_bytes())["tests"])
pool=json.load(gzip.open(tools.parent/"maps/pool.json.gz"))["tests"]
bits={n:1<<i for i,n in enumerate(names)}
for n in names:
    scope="scope:"+n.split("::")[0];bits[scope]=bits.get(scope,0)|bits[n]
everything=(1<<len(names))-1
def collect(node):
    with tempfile.TemporaryDirectory() as temp:
        env=dict(os.environ,BR_FLAG_DIR=temp,BR_FLAG_TEST=node or "",BR_EXCLUDE=json.dumps([] if node else pool),PYTHONPATH=os.pathsep.join(map(str,[tools/plugin,tools.parent,root,tools/"branch_trace"])),PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],SKIP_INTEGRATION="0",PYTHON_CPU_COUNT="2",OPENBLAS_NUM_THREADS="1",PYTHONDONTWRITEBYTECODE="1")
        run=subprocess.run([sys.executable,"-m","pytest","-q","--tb=short","-p","no:cacheprovider","-p","flag_plugin",node or "tests"],cwd=root,env=env,capture_output=True,text=True,timeout=1000)
        if run.returncode or "Error in sitecustomize" in run.stderr:raise RuntimeError(run.stdout[-3500:]+run.stderr[-700:])
        paths=list(Path(temp).glob("*.rec"))
        if not paths or node and len(paths)<2:raise ValueError("Missing workers",node)
        return [marshal.loads(gzip.decompress(p.read_bytes())) for p in paths],json.loads(Path(temp,"passed.json").read_bytes())
seen=set();events={}
with ThreadPoolExecutor(max_workers=1 if plugin=="export_trace" else 2) as workers:
    for logs,passed in workers.map(collect,[None,*pool]):
        seen.update(n.split("[",1)[0] for n in passed)
        for data in logs:
            for site,contexts in data.items():
                record=events.setdefault(site,{})
                for ctx,states in contexts.items():
                    if ctx.startswith("tests/") and ctx not in bits:continue
                    mask=bits.get(ctx,everything)
                    for state in states:record[state]=record.get(state,0)|mask
if set(names)-seen:raise ValueError("Incomplete catalog",set(names)-seen)
dest.write_bytes(gzip.compress(marshal.dumps((names,events)),mtime=0))
print(len(names),"tests",len(events),"observed sites",flush=True)
