"""Union the dependencies observed in two clean environments for each covered node."""
import gzip
import json
import sys
from pathlib import Path

left,right,catalog,destination=map(Path,sys.argv[1:])
a,b=[json.loads(gzip.decompress(p.read_bytes())) for p in (left,right)]
if set(a["catalog"])!=set(b["catalog"]):raise ValueError("test catalogs differ")
merged={}
for t in json.loads(catalog.read_bytes())["tests"]:
    file,name=t["file"],t["id"]
    u,v=a["profiles"],b["profiles"]
    if file in u and file in v:merged[file]=sorted(set(u[file])|set(v[file]))
    if name in u or name in v:
        x,y=u.get(name,u.get(file)),v.get(name,v.get(file))
        if x and y:merged[name]=sorted(set(x)|set(y))
destination.write_bytes(gzip.compress(json.dumps({"catalog":a["catalog"],"profiles":merged},separators=(",",":")).encode(),mtime=0))
