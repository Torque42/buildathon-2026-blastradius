"""Union complete validation traces and preserve earlier readers of pinned expression metadata."""
import gzip
import json
from pathlib import Path
import sys

old, mac, linux, dest = map(Path, sys.argv[1:])
earlier, *fresh = [json.loads(gzip.decompress(p.read_bytes())) for p in (old, mac, linux)]
if any(set(row["catalog"]) != set(earlier["catalog"]) for row in fresh):
    raise ValueError("Test catalogs disagree")
sites=set.intersection(*(set(row["sites"]) for row in [earlier,*fresh]))
reads={}
for record in fresh:
    for site,keys in record["readers"].items():
        for key,contexts in keys.items():
            reads.setdefault(site,{}).setdefault(key,set()).update(contexts)
for site,keys in earlier["readers"].items():
    for key,contexts in keys.items():
        live=reads.setdefault(site,{}).setdefault(key,set())
        # A direct dictionary expansion is visited while the class is declared.
        # All other contexts from the earlier profile continue to protect tests.
        copied_in={"import:"+ctx[5:].split(":")[0].replace(".","/")+".py"
                   for ctx in live if ctx.startswith("copy:")}
        live.update(ctx for ctx in contexts if ctx not in copied_in)
out={"catalog":sorted(earlier["catalog"]),"sites":sorted(sites),
     "readers":{site:{k:sorted(v) for k,v in grouped.items()} for site,grouped in reads.items()}}
dest.write_bytes(gzip.compress(json.dumps(out,separators=(",",":"),sort_keys=True).encode(),mtime=0))
print(sum(ctx.startswith("copy:") for grouped in reads.values() for values in grouped.values() for ctx in values))
