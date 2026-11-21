"""Merge platform and subprocess lookup observations per source expression and test."""
import gzip
import marshal
from pathlib import Path
import sys

mac, linux, output = map(Path,sys.argv[1:])
observed, sites, catalog = {}, set(), None
for filepath in (mac, linux):
    names, parts = marshal.loads(filepath.read_bytes())
    if catalog is None:
        catalog = names
    if catalog != names:
        raise ValueError("Catalog mismatch")
    for blob in parts:
        declared, records = marshal.loads(gzip.decompress(blob))
        sites.update(declared)
        for (site,ctx), (unknown,missing,denied,allowed) in records.items():
            key = (site, ctx.split("[",1)[0])
            result = observed.setdefault(key,[False,False,set(),None])
            result[0] |= unknown
            result[1] |= missing
            result[2].update(denied)
            if allowed is not None:
                if result[3] is None:
                    result[3]=set(allowed)
                else:
                    result[3].intersection_update(allowed)
contexts=sorted({c for _,c in observed})
bits={c:1<<i for i,c in enumerate(contexts)}
index={site:[0,0,0,{},{}] for site in sites}
for (site,ctx),(unknown,missing,denied,allowed) in observed.items():
    bit=bits[ctx]
    row=index[site]
    if unknown:
        row[0]|=bit
        continue
    if missing:row[1]|=bit
    if allowed is not None:
        row[2]|=bit
        for word in allowed-denied:row[4][word]=row[4].get(word,0)|bit
    else:
        for word in denied:row[3][word]=row[3].get(word,0)|bit
output.write_bytes(gzip.compress(marshal.dumps((catalog,contexts,index)),mtime=0))
print(len(sites),len(contexts),output.stat().st_size,flush=True)
