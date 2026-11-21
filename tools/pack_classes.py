"""Collect the actual operand types and exact equality methods seen at each comparison."""
import gzip
import marshal
from pathlib import Path
import sys

catalog=None
sites=None
observed={}
for filename in sys.argv[1:-1]:
    names,chunks=marshal.loads(Path(filename).read_bytes())
    if catalog is not None and names!=catalog:raise ValueError("Unequal catalogs")
    catalog=names
    covered=set()
    for raw in chunks:
        declared,rows=marshal.loads(gzip.decompress(raw))
        covered.update(declared)
        for site,states in rows.items():
            dest=observed.setdefault(site,{})
            for state,contexts in states.items():
                dest.setdefault(state,set()).update(contexts)
    sites=covered if sites is None else sites&covered
payload={f"{p}:{n}:{col}":[(state,sorted(contexts)) for state,contexts in observed.get((p,n,col),{}).items()]
         for p,n,col in sorted(sites)}
Path(sys.argv[-1]).write_bytes(gzip.compress(marshal.dumps((catalog,payload)),9,mtime=0))
print(len(payload),sum(map(len,payload.values())))
