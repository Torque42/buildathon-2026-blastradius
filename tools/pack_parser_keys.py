"""Preserve every observed successful, absent and opaque lookup across platforms."""
import gzip
import json
import sys
from pathlib import Path

mac,linux=[json.loads(gzip.decompress(Path(p).read_bytes())) for p in sys.argv[1:3]]
if set(mac["catalog"]) != set(linux["catalog"]):
    raise ValueError("The test catalogs differ")
rows={}
for site in sorted(set(mac["sites"]) & set(linux["sites"])):
    result={}
    for profile in (mac,linux):
        for key,uses in profile["readers"].get(site,{}).items():
            result.setdefault(key,set()).update(uses)
    rows[site]={key:sorted(uses) for key,uses in sorted(result.items())}
Path(sys.argv[3]).write_bytes(gzip.compress(json.dumps({"catalog":sorted(mac["catalog"]),"rows":rows},separators=(",",":")).encode(),mtime=0))
