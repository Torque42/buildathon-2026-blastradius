"""Union lifecycle reads from complete pristine runs on both platforms."""
import gzip
import json
from pathlib import Path
import sys

left, right, dest = map(Path, sys.argv[1:])
profiles = [json.loads(gzip.decompress(p.read_bytes())) for p in (left,right)]
base = profiles[0]
if base["catalog"] != profiles[1]["catalog"] or base["sites"].keys() != profiles[1]["sites"].keys():
    raise ValueError("The platform profiles disagree on the catalog or instrumented sites")
sites = {}
for site in base["sites"]:
    if any("$declared" not in profile["sites"][site] for profile in profiles):
        raise ValueError(site)
    sites[site] = sorted(set.union(*(set(p["sites"][site]) for p in profiles)) - {"$declared"})
result={"catalog":base["catalog"],"sites":sites}
dest.write_bytes(gzip.compress(json.dumps(result,separators=(",",":")).encode(),mtime=0))
print(len(sites),sum(not contexts for contexts in sites.values()),"never-read assignments")
