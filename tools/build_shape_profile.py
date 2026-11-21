"""Collect and validate runtime value dependencies on the unmodified SQLGlot pin."""
import argparse
from collections import defaultdict
import gzip
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


def merge(traces, dest):
    observed = defaultdict(set)
    source_reads = set()
    code_reads = defaultdict(set)
    missing, opaque, factories, arguments = defaultdict(set), set(), set(), {}
    for path in sorted(traces.glob("*.json.gz")):
        with gzip.open(path, "rt") as stream:
            trace = json.load(stream)
            for site, safe in trace["arguments"].items():
                arguments[site] = arguments.get(site, True) and safe
            for cls, keys in trace["missing"].items():
                missing[cls].update(keys)
            opaque.update(trace["opaque"])
            factories.update(trace["factories"])
            source_reads.update(trace["source_reads"])
            for p, lines in trace["code_reads"].items():
                code_reads[p].update(lines)
            for site, pairs in trace["sites"].items():
                observed[site].update(tuple(pair) for pair in pairs)
    if not observed:
        raise RuntimeError("No operand types were collected")
    data = {k: sorted(pairs) for k, pairs in observed.items()}
    data = {"arguments": arguments, "missing": {cls: sorted(keys) for cls,keys in missing.items()}, "opaque": sorted(opaque), "factories": sorted(factories), "sites": data, "source_reads": sorted(source_reads),
            "code_reads": {p: sorted(lines) for p, lines in code_reads.items()}}
    dest.write_bytes(gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), mtime=0))


def build(root, manifest, output):
    with tempfile.TemporaryDirectory() as scratch:
        traces = Path(scratch)
        hook = Path(__file__).resolve().parent / "shape_trace"
        env = dict(os.environ, BR_OPERAND_ROOT=str(root), BR_OPERAND_OUT=scratch,
                   PYTHONPATH=os.pathsep.join(map(str, (hook, root))),
                   PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                   SKIP_INTEGRATION="0", PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1")
        report = traces / "report.xml"
        subprocess.run([sys.executable, "-m", "pytest", "tests", "-q", "-p", "no:cacheprovider",
                        f"--junitxml={report}"], check=True, cwd=root, env=env)
        validate(report, manifest)
        merge(traces, output)
        data = json.loads(gzip.decompress(output.read_bytes()))
        data["catalog"] = sorted(row["id"] for row in json.loads(manifest.read_bytes())["tests"])
        output.write_bytes(gzip.compress(json.dumps(data,sort_keys=True,separators=(",",":")).encode(),mtime=0))


def validate(report, manifest):
    # Require each numbered starter test (one number can cover multiple pytest
    # parameterizations) to have been executed successfully.
    seen = set()
    for case in ET.parse(report).iter("testcase"):
        if any(case.find(tag) is not None for tag in ("error", "failure", "skipped")):
            continue
        parts = case.get("classname", "").split(".")
        for i, part in enumerate(parts):
            if part.startswith("test_") and i and (i + 1 == len(parts) or not parts[i + 1].startswith("test_")):
                seen.add("/".join(parts[:i + 1]) + ".py::" + "::".join(parts[i + 1:] + [case.attrib["name"].split("[")[0]]))
                break
    expected = {row["id"].split("[")[0] for row in json.loads(manifest.read_text())["tests"]}
    if expected - seen:
        raise RuntimeError(f"Missing test outcomes: {len(expected - seen)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("tests_json", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    build(args.source.resolve(), args.tests_json, args.output)
