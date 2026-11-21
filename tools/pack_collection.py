"""Union independently collected complete pytest module reachability from both platforms."""
from pathlib import Path
import gzip
import marshal
import sys
merged = {}
outside = {}
catalog = None
def update(target,source):
    for path,mask in source.items():
        target[path]=target.get(path,0)|mask
for filename in sys.argv[1:-1]:
    names,unknown,files=marshal.loads(gzip.decompress(Path(filename).read_bytes()))
    if catalog is not None and (names != catalog or set(files) != set(merged)):
        raise ValueError("Mismatched test catalog or test files")
    catalog=names
    update(outside,unknown)
    for name,row in files.items():
        update(merged.setdefault(name,{}),row)
Path(sys.argv[-1]).write_bytes(gzip.compress(marshal.dumps((catalog,outside,merged)),9,mtime=0))
