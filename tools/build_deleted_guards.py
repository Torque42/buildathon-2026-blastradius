"""Generate the guard-site manifest and pack validated pristine pytest process traces."""
import ast
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deleted_guards import catalog_key, guard_sites


def manifest(source):
    entries = []
    for path in sorted((source / "sqlglot").rglob("*.py")):
        raw = path.read_text("utf-8")
        lines = raw.splitlines()
        rel = path.relative_to(source).as_posix()
        for node, owner, spec in guard_sites(ast.parse(raw, filename=rel)):
            if lines[node.lineno - 1].lstrip().startswith("if "):
                entries.append((rel, node.lineno, owner.name, spec))
    return sorted(entries)


def cold(source, records, pool):
    """Give shared worker pools a single possible test owner from interpreter startup."""
    tests = json.loads(gzip.decompress(pool.read_bytes()))["tests"]
    for index, name in enumerate(tests):
        directory = (records / f"cold-{index}").resolve()
        directory.mkdir()
        (directory / "manifest.json").symlink_to((records / "manifest.json").resolve())
        (directory / "owner").write_text(name)
        env = dict(os.environ, BR_GUARD_TEST=name, BR_GUARD_OUT=str(directory))
        with (directory / "pytest.log").open("w") as output:
            subprocess.run([sys.executable, "-m", "pytest", name, "-q", "-p", "no:cacheprovider",
                            "-p", "guard_plugin", "--timeout=220"], cwd=source, env=env,
                           stdout=output, stderr=subprocess.STDOUT, timeout=245, check=True)


def completed(records):
    starts = {p.stem for p in records.glob("*.start")}
    ends = {p.stem for p in records.glob("*.end")}
    if not starts or starts != ends:
        raise ValueError(f"Incomplete child process traces: {records.name} {starts ^ ends}")
    return starts


def pack(source, records, catalog, pool, dest):
    names = sorted(test["id"] for test in json.loads(catalog.read_bytes())["tests"])
    numbers = {name: i for i, name in enumerate(names)}
    suite = json.loads((records / "suite.json").read_bytes())
    if (suite["status"] or suite["failed"] or suite["collection_errors"] or set(names) - set(suite["passed"])
            or set(names) & set(suite["skipped"])):
        raise ValueError("Incomplete or failing pristine suite")
    starts = completed(records)
    sites = json.loads((records / "manifest.json").read_bytes())
    if sites != json.loads(json.dumps(manifest(source))):
        raise ValueError("The instrumented guard manifest differs from the pinned source")
    pool_ids = set(json.loads(gzip.decompress(pool.read_bytes()))["tests"])
    verified_pool = set()
    seen = [set() for _ in sites]
    keep = [set() for _ in sites]
    shared = set()
    for pid in starts:
        reached, retained = json.loads((records / (pid + ".end")).read_bytes())
        for context, site in reached:
            if context in numbers and context not in pool_ids:
                seen[site].add(numbers[context])
        for context, site in retained:
            if context in numbers:
                keep[site].add(numbers[context])
            else:
                shared.add(site)
    for directory in records.glob("cold-*"):
        name = (directory / "owner").read_text()
        if name not in pool_ids or name in verified_pool:
            raise ValueError(f"Unexpected isolated pool-test owner {name}")
        report = json.loads((directory / "suite.json").read_bytes())
        workers = completed(directory)
        if (len(workers) < 2 or report["status"] or name not in report["passed"] or
                report["failed"] or report["collection_errors"] or name in report["skipped"]):
            continue
        verified_pool.add(name)
        for pid in workers:
            reached, retained = json.loads((directory / (pid + ".end")).read_bytes())
            for _, site in reached:
                seen[site].add(numbers[name])
            for _, site in retained:
                keep[site].add(numbers[name])
    packed = {}
    for i, (path, line, owner, spec) in enumerate(sites):
        if seen[i] and i not in shared:
            packed.setdefault(path, {})[line] = [sorted(seen[i]), sorted(keep[i])]
    stored = {"catalog": catalog_key(names), "names": names, "sites": packed,
              "sources": {p: hashlib.sha256((source / p).read_text().encode()).hexdigest() for p in packed}}
    dest.write_bytes(gzip.compress(json.dumps(stored, separators=(",", ":")).encode(), mtime=0))
    print(f"Packed {sum(map(len, packed.values()))}/{len(sites)} sites from {len(starts)} processes",
          f"with {len(names)} passing tests. Pool tests verified independently: {len(verified_pool)}/{len(pool_ids)}",
          file=sys.stderr)


if __name__ == "__main__":
    mode, *args = sys.argv[1:]
    paths = [Path(arg) for arg in args]
    if mode == "manifest":
        paths[1].write_text(json.dumps(manifest(paths[0])))
    elif mode == "cold":
        cold(*paths)
    elif mode == "pack":
        pack(*paths)
    else:
        raise SystemExit("Use manifest SOURCE OUTPUT or pack SOURCE RECORDS TESTS POOL OUTPUT")
