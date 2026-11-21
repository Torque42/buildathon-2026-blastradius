import gzip,json,os,subprocess,sys,time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
repo=Path(__file__).resolve().parents[1]
source,manifest,dest=map(lambda p:Path(p).resolve(),sys.argv[1:4])
chosen=sorted(json.loads(manifest.read_bytes())["tests"],key=lambda t:-t["seconds"])[:int(sys.argv[4])]
def collect(t):
    node=t["id"];out=dest/str(t["number"]);out.mkdir(parents=True,exist_ok=True)
    if (out/"node").exists():return
    env=dict(os.environ,PYTHONPATH=os.pathsep.join(map(str,[repo/"tools"/os.environ.get("BR_TRACE_PLUGIN","predicate_trace"),repo,source])),PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],BR_OUT=str(out),BR_SOURCE=str(source),PYTHONHASHSEED="901",SKIP_INTEGRATION="0",OPENBLAS_NUM_THREADS="1",PYTHON_CPU_COUNT="2")
    with (out/"pytest.log").open("w") as stream:
        proc=subprocess.run([sys.executable,"-m","pytest",node,"-q","--tb=short","-p","no:cacheprovider"],cwd=source,env=env,stdout=stream,stderr=stream)
    log=(out/"pytest.log").read_text()
    if not proc.returncode and "1 passed" in log and "Error in sitecustomize" not in log and list(out.glob("*.gz")):
        (out/"node").write_text(node)
    print(node,proc.returncode,flush=True)
with ThreadPoolExecutor(max_workers=3) as pool:list(pool.map(collect,chosen))
