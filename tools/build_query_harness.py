"""Get cold harness coverage from the actual upstream tests including spawned processes."""
import gzip
import marshal
import os
import subprocess
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve()
destination = Path(sys.argv[2])
tools = Path(__file__).resolve().parent
result = {}
for kind in ("tpch", "tpcds"):
    node = "tests/test_executor.py::TestExecutor::test_execute_"+kind
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, BR_QUERY_PIN=str(root), BR_QUERY_OUT=tmp, PYTHONPATH=os.pathsep.join(map(str,(tools/"query_harness", root))),
                   PATH=str(Path(sys.executable).parent)+os.pathsep+os.environ["PATH"], SKIP_INTEGRATION="0",
                   PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1")
        p = subprocess.run([sys.executable,"-m","pytest","-q","-p","no:cacheprovider",node],env=env,cwd=root,capture_output=True)
        if p.returncode or b" passed" not in p.stdout:
            raise RuntimeError(node,p.stdout[-1200:],p.stderr[-400:])
        union = {}
        wrapped_count = 0
        for path in Path(tmp).glob("*.gz"):
            wrapped, rows = marshal.loads(gzip.decompress(path.read_bytes()))
            wrapped_count += wrapped
            for key, bits in rows.items():
                union[key] = union.get(key,0) | bits
        if wrapped_count < 2 or not union:
            raise RuntimeError("Missing cold test or child",node,wrapped_count)
        result[kind] = union
destination.write_bytes(gzip.compress(marshal.dumps(result),mtime=0))
