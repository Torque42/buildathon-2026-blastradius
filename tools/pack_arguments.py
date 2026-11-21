"""Union observations from complete pristine runs and keep sites registered on both platforms."""
import gzip
import marshal
import sys
from pathlib import Path

catalog = None
covered = None
observed = {}
for filename in sys.argv[1:-1]:
    names, chunks = marshal.loads(Path(filename).read_bytes())
    if catalog is not None and catalog != names:
        raise ValueError("Incompatible test catalogs")
    catalog = names
    declared = set()
    for chunk in chunks:
        sites, rows = marshal.loads(gzip.decompress(chunk))
        declared.update(sites)
        for site, contexts in rows.items():
            observed.setdefault(site, set()).update(contexts)
    covered = declared if covered is None else covered & declared
contexts = sorted({ctx for site in covered for ctx in observed.get(site, ())})
bits = {name: 1 << i for i, name in enumerate(contexts)}
sites = {}
for path, line, start, end, kind in sorted(covered):
    readers = observed.get((path, line, start, end, kind), ())
    if "*" in readers:
        continue
    sites.setdefault(path, {}).setdefault(line, []).append(
        (start, end, kind, sum(bits[ctx] for ctx in readers)))
raw = marshal.dumps((catalog, contexts, sites))
out = Path(sys.argv[-1])
out.write_bytes(gzip.compress(raw, mtime=0))
print(len(covered), sum(len(rows) for files in sites.values() for rows in files.values()),
      len(contexts), out.stat().st_size)
