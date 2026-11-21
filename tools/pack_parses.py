"""Keep fully observed test runs and cross-platform call input, output and reachability."""
import gzip
import lzma
import marshal
import sys
from pathlib import Path

folders = [Path(p) for p in sys.argv[1:-1]]
datasets = [{p.name: p for p in folder.glob("*.gz")} for folder in folders]
common = set.intersection(*(set(rows) for rows in datasets))
def merge(target, source):
    for path, mask in source.items():
        target[path] = target.get(path,0) | mask
all_calls = {}
tests = {}
for filename in sorted(common):
    name = None
    outside = {}
    observed = set()
    for dataset in datasets:
        label, chunks = marshal.loads(gzip.decompress(dataset[filename].read_bytes()))
        if name is not None and name != label:
            raise ValueError("Test names do not match")
        name=label
        for complete, unknown, calls in chunks:
            if not complete:raise ValueError("Incomplete parse trace")
            merge(outside, unknown)
            for call, coverage in calls.items():
                observed.add(call)
                merge(all_calls.setdefault(call,{}), coverage)
    tests[name] = outside, observed
# Cross-platform disagreement for one input prevents the associated proof.
by_input={}
for key,digest in all_calls:
    by_input.setdefault(key,set()).add(digest)
stable = sorted((pair for pair in all_calls if len(by_input[pair[0]])==1),key=lambda x:repr(x[0]))
ids = {key:i for i,key in enumerate(stable)}
lines = {}
for pair, index in ids.items():
    for path, mask in all_calls[pair].items():
        target=lines.setdefault(path,{})
        while mask:
            bit=mask&-mask
            mask^=bit
            line=bit.bit_length()-1
            target[line]=target.get(line,0)|(1<<index)
out={}
for name,(outside,calls) in tests.items():
    available=0
    for pair in calls:
        if pair in ids:available |= 1<<ids[pair]
        else:merge(outside,all_calls[pair])
    out[name]=outside,available
dst=Path(sys.argv[-1])
dst.write_bytes(lzma.compress(marshal.dumps((stable,lines,out)),preset=8))
print(len(out),len(stable),sum(map(len,lines.values())),dst.stat().st_size)
