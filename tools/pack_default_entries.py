"""Retain cold function-entry evidence shared by independently successful platform profiles."""
import gzip
import marshal
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
samples=[marshal.loads(gzip.decompress(Path(p).read_bytes())) for p in sys.argv[1:]]
known={n:i for i,n in enumerate(sorted(set.intersection(*(set(k) for k,_ in samples))))}
columns={}
for names,paths in samples:
    for path,(_,owners) in paths.items():
        combined=columns.setdefault(path,{})
        for visited,ids in owners.items():
            for name,idx in known.items():
                if ids & (1<<names[name]):combined[idx]=combined.get(idx,0)|visited
compressed={}
for path,each in columns.items():
    groups={}
    for idx,visited in each.items():groups[visited]=groups.get(visited,0)|(1<<idx)
    compressed[path]=groups
(root/"maps/function-entries.gz").write_bytes(gzip.compress(marshal.dumps((known,compressed)),mtime=0))
print(len(known))
