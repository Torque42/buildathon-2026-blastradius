import gzip,json,marshal,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
base,manifest=map(Path,sys.argv[1:3])
catalog=[t["id"] for t in json.loads(manifest.read_bytes())["tests"]]
sites={}
for d in base.iterdir():
    if not (d/"node").exists():continue
    name=(d/"node").read_text(); bit=1<<catalog.index(name)
    for f in d.glob("*.gz"):
        registered,states=json.loads(gzip.decompress(f.read_bytes()))
        for key in registered:
            record=sites.setdefault(key,[0,{}])
            record[0]|=bit
        for key,rows in states.items():
            masks=sites[key][1]
            for state in rows:
                k=tuple(state) if state is not None else None
                masks[k]=masks.get(k,0)|bit
(root/"maps/conditions.gz").write_bytes(gzip.compress(marshal.dumps((catalog,sites)),mtime=0))
print(len(sites))
