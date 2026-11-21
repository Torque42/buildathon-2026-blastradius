"""Measure per-test SQL parse call contexts and all uncaptured parser instructions on the pin."""
from concurrent.futures import ThreadPoolExecutor
import ast
import gzip
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile

tools=Path(__file__).resolve().parent
pin,manifest,dest=map(lambda p:Path(p).resolve(),sys.argv[1:4])
dest.mkdir(parents=True,exist_ok=True)
tests=json.loads(manifest.read_bytes())['tests']
pool=set(json.load(gzip.open(tools.parent/'maps/pool.json.gz'))['tests'])
deny={'patch','mock','assertLogs','assertWarns','patch_object','setattr','delattr','catch_warnings','filterwarnings','captureWarnings','redirect_stderr','redirect_stdout','__dict__','register','_classes','tracemalloc','monkeypatch'}
def suitable(t):
    parts=t['id'].split('::')
    if len(parts)!=3 or t['seconds']<0.13 or parts[-1] in ('test_max_nodes','test_generator_memory'):
        return False
    tree=ast.parse((pin/parts[0]).read_bytes())
    c=next((x for x in tree.body if isinstance(x,ast.ClassDef) and x.name==parts[1]),None)
    if c is None:return False
    method=next((x for x in c.body if isinstance(x,ast.FunctionDef) and x.name==parts[2]),None)
    if method is None:return False
    return not any((isinstance(n,ast.Name) and n.id in deny) or (isinstance(n,ast.Attribute) and n.attr in deny) for n in ast.walk(method))
def run(t):
    name=t['id']; out=dest/(str(t['number'])+'.gz')
    if out.exists():return
    with tempfile.TemporaryDirectory() as tmp:
        env=dict(os.environ, BR_PARSE_DIR=tmp,BR_PARSE_PIN=str(pin),SKIP_INTEGRATION='0',
                 PYTHON_CPU_COUNT='2', OPENBLAS_NUM_THREADS='1', PYTHONDONTWRITEBYTECODE='1',
                 PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ['PATH'],
                 PYTHONPATH=os.pathsep.join(map(str,[tools/'parse_trace',tools.parent,pin])))
        proc=subprocess.run([sys.executable,'-m','pytest',name,'-q','--tb=no','-p','no:cacheprovider'],cwd=pin,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=360)
        if proc.returncode or '1 passed' not in proc.stdout:
            print('skip',name,proc.returncode,proc.stdout[:700],proc.stdout[-250:],flush=True);return
        data=[marshal.loads(gzip.decompress(p.read_bytes())) for p in Path(tmp).glob('*.gz')]
        # multiprocessing may spawn a resource tracker that never imports SQLGlot.
        data=[row for row in data if row[0] or row[1] or row[2]]
        if not data or (name in pool and len(data)<2) or any(not row[0] for row in data):
            print('skip incomplete',name,[(r[0],len(r[1]),len(r[2])) for r in data],flush=True);return
        out.write_bytes(gzip.compress(marshal.dumps((name,data)),3,mtime=0))
        print(name,sum(len(x[2]) for x in data),out.stat().st_size,flush=True)
with ThreadPoolExecutor(max_workers=int(os.getenv('BR_PARSE_WORKERS','2'))) as e:
    list(e.map(run,[t for t in sorted(tests,key=lambda x:-x['seconds']) if suitable(t)]))
