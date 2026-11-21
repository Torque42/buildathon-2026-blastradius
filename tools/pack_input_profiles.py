"""Combine observations from each reference platform before making exclusions."""
import gzip
import json
import sys
from pathlib import Path

inputs = [json.loads(gzip.decompress(Path(v).read_bytes())) for v in sys.argv[2:]]
catalog = set.intersection(*({n for ids in p["catalog"].values() for n in ids} for p in inputs))
sites = inputs[0]["sites"]
guard_sites = inputs[0]["guard_sites"]
assert all(p["sites"] == sites and p["guard_sites"] == guard_sites for p in inputs)
reads = {}
guards = {}
states = {}
for record in inputs:
    for key, ids in record["reads"].items():
        reads.setdefault(key, set()).update(ids)
    for key, files in record["guards"].items():
        for file, snapshots in files.items():
            for snapshot in snapshots:
                state = tuple(snapshot)
                index = states.setdefault(state, len(states))
                guards.setdefault(key, {}).setdefault(file, set()).add(index)
payload = dict(catalog=sorted(catalog), sites=sites, guard_sites=guard_sites,
               reads={k:sorted(v) for k,v in reads.items()}, states=list(states),
               guards={site:{f:sorted(v) for f,v in files.items()} for site, files in guards.items()})
Path(sys.argv[1]).write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
