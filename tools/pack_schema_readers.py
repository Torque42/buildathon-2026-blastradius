"""Merge cold runtime schema observations from the two profiling platforms."""
import gzip
import json
import sys
from pathlib import Path

first, second = [json.loads(gzip.decompress(Path(p).read_bytes())) for p in sys.argv[1:3]]
if set(first["catalog"]) != set(second["catalog"]):
    raise ValueError("The profiling catalogs differ")
sites = sorted(set(first["sites"]) & set(second["sites"]))
rows = {}
for site in sites:
    contexts = set()
    for profile in (first, second):
        contexts.update(profile["readers"].get(site, {}).get("*", []))
    rows[site] = sorted(contexts)
payload = {"catalog": sorted(first["catalog"]), "rows": rows}
Path(sys.argv[3]).write_bytes(gzip.compress(json.dumps(payload, separators=(",", ":")).encode(), mtime=0))
