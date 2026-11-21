"""Run complete pristine tests and record lookup keys, including misses and child processes."""
from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    tools = Path(__file__).resolve().parent
    source, manifest, output = map(lambda arg: Path(arg).resolve(), sys.argv[1:4])
    pool = json.loads(gzip.decompress((tools.parent / "maps/pool.json.gz").read_bytes()))["tests"]
    catalog = {t["id"] for t in json.loads(manifest.read_bytes())["tests"]}
    passed, combined = set(), {}

    def run(test):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                       PYTHONPATH=os.pathsep.join(map(str, [tools / "lexical_trace", tools / "table_trace", source])),
                       BR_FLAG_DIR=tmp, PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1",
                       SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1")
            if test:
                env["BR_FLAG_TEST"] = test
            else:
                env["BR_EXCLUDE"] = json.dumps(pool)
            result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                                     "-p", "flag_plugin", test or "tests"], cwd=source, env=env,
                                    capture_output=True, text=True)
            if result.returncode:
                raise RuntimeError(result.stdout[-2000:] + result.stderr[-1000:])
            done = json.loads((Path(tmp)/"passed.json").read_bytes())
            logs = [json.loads(gzip.decompress(p.read_bytes())) for p in Path(tmp).glob("*.json.gz")]
            if not any(len(init) == 2 for init, _ in logs):
                raise RuntimeError(f"Incomplete instrumentation for {test}")
            if test and len(logs) < 2:
                raise RuntimeError(f"Missing spawned process trace for {test}")
            return done, logs

    with ThreadPoolExecutor(max_workers=3) as jobs:
        for done, logs in jobs.map(run, [None, *pool]):
            passed.update(node.split("[",1)[0] for node in done)
            for _, rows in logs:
                for site, context, words in rows:
                    combined.setdefault(site, {}).setdefault(context.split("[",1)[0], set()).update(words)
    if not catalog <= passed:
        raise ValueError(sorted(catalog-passed))
    data = {"catalog":sorted(catalog), "sites":{site:{name:sorted(words) for name,words in rows.items()} for site,rows in combined.items()}}
    output.write_bytes(gzip.compress(json.dumps(data,separators=(",",":")).encode(),mtime=0))
    print(len(catalog), len(combined), output.stat().st_size, flush=True)


if __name__ == "__main__":
    main()
