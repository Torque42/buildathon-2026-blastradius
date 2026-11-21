"""Pack platform-complete cases in which equality can differ from identity."""
import gzip
import marshal
from pathlib import Path
import sys

catalog = None
covered = None
readers = {}
for filename in sys.argv[1:-1]:
    names, chunks = marshal.loads(Path(filename).read_bytes())
    if catalog is not None and catalog != names:
        raise ValueError("Test catalogs differ")
    catalog = names
    sites = set()
    for raw in chunks:
        registered, may = marshal.loads(gzip.decompress(raw))
        sites.update(registered)
        for site, contexts in may.items():
            readers.setdefault(site,set()).update(contexts)
    covered = sites if covered is None else covered & sites
contexts = sorted({ctx for site in covered for ctx in readers.get(site,())})
bit = {ctx:1<<index for index, ctx in enumerate(contexts)}
result = {f"{path}:{line}:{column}":sum(bit[ctx] for ctx in readers.get((path,line,column),()))
          for path,line,column in sorted(covered) if "*" not in readers.get((path,line,column),())}
dst = Path(sys.argv[-1])
dst.write_bytes(gzip.compress(marshal.dumps((catalog,contexts,result)),mtime=0))
print(len(result), "sites", len(contexts), "contexts", dst.stat().st_size, "bytes")
