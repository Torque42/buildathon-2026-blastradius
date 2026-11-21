"""Union test observations and retain only sites reached on both platforms."""
import gzip,marshal,sys
from pathlib import Path
(a,x),(b,y)=[marshal.loads(gzip.decompress(Path(p).read_bytes())) for p in sys.argv[1:3]]
if a!=b:raise ValueError("Catalog mismatch")
records={}
for site in x.keys()&y.keys():
    left,right=x[site],y[site]
    records[site]={s:left.get(s,0)|right.get(s,0) for s in left.keys()|right.keys()}
Path(sys.argv[3]).write_bytes(gzip.compress(marshal.dumps((a,records)),mtime=0))
