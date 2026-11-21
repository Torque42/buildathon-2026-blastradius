"""Require both pristine stage executions to have observed a conditional."""
import gzip,marshal,sys,tarfile
from pathlib import Path
first=Path(sys.argv[1]);second=Path(sys.argv[2]);dest=Path(sys.argv[3])
p=next(first.glob("*.rec"));mac=marshal.loads(gzip.decompress(p.read_bytes()))
with tarfile.open(second) as a:
    raw=next(m for m in a.getmembers() if m.isfile() and m.name.endswith(".rec"))
    linux=marshal.loads(gzip.decompress(a.extractfile(raw).read()))
labels,_,=marshal.loads(gzip.decompress((Path(__file__).resolve().parents[1]/"maps/query-phases.gz").read_bytes()))
bits={label:1<<i for i,label in enumerate(labels) if len(label)==3 and label[-1] not in ("harness","outer")}
out={}
for site in mac.keys() & linux.keys():
    known=0;states={}
    for label in mac[site].keys()&linux[site].keys()&bits.keys():
        mask=bits[label]
        obs=mac[site][label]|linux[site][label]
        if not obs or None in obs:continue
        known|=mask
        for row in obs:states[row]=states.get(row,0)|mask
    if known:out[site]=(known,states)
dest.write_bytes(gzip.compress(marshal.dumps((labels,out)),mtime=0))
print("observed",len(out),"conditional sites in",len(labels),"stages")
