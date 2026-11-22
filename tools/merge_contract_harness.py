"""Merge the parent and child-process reads outside the modeled SQL stages."""

import gzip
import marshal
from pathlib import Path
import sys

base=Path(sys.argv[1])
out={}
for kind in ("tpch","tpcds"):
    paths=list((base/kind).glob("*.gz"))
    if len(paths)<2:
        raise ValueError("Expected separate test and process-pool observations")
    total=set()
    for path in paths:
        ver,keys,rows=marshal.loads(gzip.decompress(path.read_bytes()))
        if tuple(ver)!=sys.version_info[:2]:
            raise ValueError("version")
        for label,ids in rows.items():
            if tuple(label)!=(kind,-1,"harness"):
                raise ValueError(label)
            total.update(tuple(keys[i]) for i in ids)
    if not total:
        raise ValueError("empty trace")
    out[kind]=sorted(total,key=repr)
    print(kind,len(paths),len(total),file=sys.stderr)
sys.stdout.buffer.write(gzip.compress(marshal.dumps(out),mtime=0))
