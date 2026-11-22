"""Collect, verify and invert per-test read sets on the unmodified contest pin."""

import gzip
import json
import lzma
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    tools=Path(__file__).resolve().parent
    root,manifest,output=map(Path,sys.argv[1:4])
    catalog=sorted(t["id"] for t in json.loads(manifest.read_bytes())["tests"])
    bits={node:1<<i for i,node in enumerate(catalog)}
    all_bits=(1<<len(catalog))-1
    for node in catalog:
        scope="scope:"+node.split("::")[0]
        bits[scope]=bits.get(scope,0)|bits[node]
    pool=json.loads(gzip.decompress((tools.parent/"maps/pool.json.gz").read_bytes()))["tests"]
    selected=set()
    events={}
    for node in [None,*pool]:
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,BR_FLAG_DIR=tmp,BR_FLAG_TEST=node or "",
                     BR_EXCLUDE=json.dumps([] if node else pool),PYTHONPATH=os.pathsep.join(map(str,[
                         tools/"tokenizer_trace",tools/"table_trace",root])),
                     OPENBLAS_NUM_THREADS="1",PYTHON_CPU_COUNT="2",SKIP_INTEGRATION="0",PYTHONDONTWRITEBYTECODE="1")
            proc=subprocess.run([sys.executable,"-m","pytest","-q","--tb=short","-p","no:cacheprovider",
                                 "-p","flag_plugin",node or "tests"],cwd=root,env=env,
                                capture_output=True,text=True,timeout=760)
            if proc.returncode:
                raise RuntimeError((node,proc.stdout[-3000:],proc.stderr[-900:]))
            passed=json.loads((Path(tmp)/"passed.json").read_bytes())
            selected.update(t.split("[",1)[0] for t in passed)
            captures=list(Path(tmp).glob("*.reads"))
            if len(captures)<(2 if node else 1):
                raise RuntimeError(("Incomplete subprocess coverage",node))
            ok=0
            for capture in captures:
                valid,rows=marshal.loads(gzip.decompress(capture.read_bytes()))
                ok+=valid
                for (owner,field,ctx),operations in rows.items():
                    mask=bits.get(ctx.split("[",1)[0],bits[node] if node else all_bits)
                    group=events.setdefault((owner,field),{})
                    for operation in operations:
                        group[operation]=group.get(operation,0)|mask
            if not ok:
                raise RuntimeError(("Instrumentation was absent",node))
            print(node or "shared suite",len(passed),len(captures),flush=True)
    if set(catalog)-selected:
        raise ValueError(("Missing tests",set(catalog)-selected))
    # Intern the many repeated context bitsets before compression.
    masks={}
    ranges=[]
    packed={}
    for site,ops in events.items():
        rows={}
        for reading,value in ops.items():
            if value not in masks:
                masks[value]=len(ranges)
                ranges.append(value)
            rows[reading]=masks[value]
        packed[site]=rows
    output.write_bytes(lzma.compress(marshal.dumps((catalog,ranges,packed),2),preset=3))
    print("Covered",len(catalog),"tests,",len(events),"concrete tables,",sum(map(len,packed.values())),
          "distinct reads,",output.stat().st_size,"bytes",flush=True)


if __name__=="__main__":
    main()
