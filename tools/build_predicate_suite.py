import gzip,json,os,sys,subprocess
from pathlib import Path
r=Path(__file__).resolve().parents[1]
source,tests,dest=map(lambda s:Path(s).resolve(),sys.argv[1:4])
dest.mkdir(exist_ok=True,parents=True)
pool=json.loads(gzip.decompress((r/"maps/pool.json.gz").read_bytes()))["tests"]
env=dict(os.environ,BR_SOURCE=str(source),BR_TESTS=str(tests),BR_EXCLUDE=json.dumps(pool),BR_OUT=str(dest),PYTHONPATH=os.pathsep.join(map(str,[r/"tools/predicate_trace",r,source])),PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],SKIP_INTEGRATION="0",PYTHON_CPU_COUNT="2",OPENBLAS_NUM_THREADS="1")
subprocess.run([sys.executable,"-m","pytest","tests","-p","profile_plugin","-p","no:cacheprovider","-q","--tb=short"],env=env,cwd=source,check=True)
