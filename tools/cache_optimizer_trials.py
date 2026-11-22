"""Record the passing original optimizer methods and their immutable database inputs."""

import marshal
import os
from pathlib import Path
import sys
import time

root=Path(__file__).resolve().parents[1]
pin=root/"cache/pinned"
sys.path[:0]=[str(root),str(pin)]
os.chdir(pin)
os.environ["SKIP_INTEGRATION"]="0"
import optimizer_trials as trials

boundary=trials.Boundary()
module=trials.load(pin,boundary)
result={}
for name in sorted(vars(module.TestOptimizer)):
    if not name.startswith("test_"):
        continue
    start=time.process_time()
    try:
        evidence=trials.execute(module,boundary,name)
        if evidence:
            result[name]=(max(0.0002,time.process_time()-start),evidence)
    except Exception:
        continue
if not result:
    raise RuntimeError("No original optimizer assertions were verified")
(root/"cache/optimizer-trials").write_bytes(marshal.dumps(result))
print("Verified",len(result),"original optimizer tests,",sum(v[1][-1] for v in result.values()),
      "complete optimizer worker calls and",sum(sum(c for _,c in v[1][2]) for v in result.values()),"database comparisons",flush=True)
