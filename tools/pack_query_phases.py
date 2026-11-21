"""Union phase-level and opaque harness coverage from the two pristine platforms."""
import gzip
import marshal
import sys
from pathlib import Path

def load(name):
    return marshal.loads(gzip.decompress(Path(name).read_bytes()))

first, second, h1, h2, destination = sys.argv[1:]
labels, mac = load(first)
other_labels, linux = load(second)
if labels != other_labels:
    raise ValueError("The platforms disagree about the query stages")
locations = {}
for coverage in (mac, linux):
    for (path, name, line, signature), groups in coverage.items():
        member = locations.setdefault((path, name, line), {}).setdefault(signature, {})
        for offsets, stages in groups.items():
            member[offsets] = member.get(offsets,0) | stages
for harness in (load(h1), load(h2)):
    for kind, coverage in harness.items():
        label = (kind, -1, "harness")
        if label not in labels:
            labels.append(label)
        bit = 1 << labels.index(label)
        for (path, name, line, signature), offsets in coverage.items():
            member = locations.setdefault((path,name,line), {}).setdefault(signature,{})
            member[offsets] = member.get(offsets,0) | bit
Path(destination).write_bytes(gzip.compress(marshal.dumps((labels,locations)),mtime=0))
