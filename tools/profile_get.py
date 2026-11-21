"""Capture builtin dictionary lookup results per test on a complete pristine run."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def collect():
    tools=Path(__file__).resolve().parent
    source, manifest, output = map(lambda arg:Path(arg).resolve(),sys.argv[1:])
    pool=json.loads(gzip.decompress((tools.parent/"maps/pool.json.gz").read_bytes()))["tests"]
    catalog={t["id"] for t in json.loads(manifest.read_bytes())["tests"]}
    seen=set()
    combined=[]

    def run(test):
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"],
                     BR_FLAG_DIR=tmp, BR_LOOKUP_PIN=str(source), PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1",
                     SKIP_INTEGRATION="0",PYTHONDONTWRITEBYTECODE="1",
                     PYTHONPATH=os.pathsep.join(map(str,[tools/"lookup_trace",tools/"table_trace",source])))
            if test:env["BR_FLAG_TEST"]=test
            else:env["BR_EXCLUDE"]=json.dumps(pool)
            proc=subprocess.run([sys.executable,"-m","pytest","-q","-p","no:cacheprovider","-p","flag_plugin",test or "tests"],
                                cwd=source,env=env,capture_output=True,text=True)
            if proc.returncode:raise RuntimeError(proc.stdout[-3000:]+proc.stderr[-1500:])
            raw=[p.read_bytes() for p in Path(tmp).glob("*.bin.gz")]
            if not raw or (test and len(raw)<2):raise ValueError(f"Missing trace: {test}")
            return json.loads((Path(tmp)/"passed.json").read_bytes()),raw

    with ThreadPoolExecutor(max_workers=3) as jobs:
        for done,raw in jobs.map(run,[None,*pool]):
            seen.update(n.split("[",1)[0] for n in done)
            combined.extend(raw)
    if not catalog<=seen:raise ValueError(sorted(catalog-seen))
    output.write_bytes(marshal.dumps((sorted(catalog),combined)))
    print(len(seen),len(combined),output.stat().st_size,flush=True)


if __name__=="__main__":
    collect()
