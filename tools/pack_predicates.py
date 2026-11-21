import gzip,json,marshal,sys
from pathlib import Path
from collections import defaultdict
root=Path(__file__).resolve().parents[1]
individual,suite,manifest=map(Path,sys.argv[1:4])
catalog=[t["id"] for t in json.loads(manifest.read_bytes())["tests"]]
bits={key:1<<i for i,key in enumerate(catalog)}
sites={}
def row(key):
    if key not in sites:sites[key]=[0,0,{},{}]
    return sites[key]
def ingest(registered,seen,cmp,mask):
    for k in registered:row(k)[0]|=mask
    for k in seen:row(k)[1]|=mask
    for k,(relations,vals) in cmp.items():
        v=row(k);v[1]|=mask
        for rel,identity in relations:
            key=(rel,identity);v[2][key]=v[2].get(key,0)|mask
        for raw in vals:
            key=tuple(raw) if isinstance(raw,list) else raw
            v[3][key]=v[3].get(key,0)|mask
solo=set()
for d in individual.iterdir():
    if not (d/"node").exists():continue
    node=(d/"node").read_text()
    if node not in bits:continue
    for f in d.glob("*.gz"):ingest(*json.loads(gzip.decompress(f.read_bytes())),bits[node])
    solo.add(node)
others=0
if suite.exists():
    for f in suite.glob("*.partial.gz"):
        node,seen,cmp=json.loads(gzip.decompress(f.read_bytes()))
        if node in solo:continue
        mask=bits[node]
        others|=mask;ingest([],seen,cmp,mask)
    unknown=set()
    for f in suite.glob("*.gz"):
        if f.name.endswith(".partial.gz"):continue
        reg,seen,cmp=json.loads(gzip.decompress(f.read_bytes()))
        # Observations during import/setup or in an unattributed child may
        # affect any ordinary test. Keep all such tests for those sites.
        ingest(reg,[],{},others)
        unknown.update(seen);unknown.update(cmp)
    for key in unknown:row(key)[0]&=~others
out=root/"maps/predicate-observations.gz"
out.write_bytes(gzip.compress(marshal.dumps((catalog,sites)),mtime=0))
print(len(sites),len(solo),others.bit_count(),out.stat().st_size)
