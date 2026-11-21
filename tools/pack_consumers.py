"""Union measured readers on both systems. Unknown scopes keep all tests."""
import gzip
import json
import marshal
import sys
from pathlib import Path
a,b=[json.loads(gzip.decompress(Path(f).read_bytes())) for f in sys.argv[1:3]]
catalog=a["catalog"]
if b["catalog"]!=catalog:raise ValueError("Conflicting test catalogs")
numbers={n:1<<i for i,n in enumerate(catalog)}
for n in catalog:
    scope="scope:"+n.split("::")[0]
    numbers[scope]=numbers.get(scope,0)|numbers[n]
all_tests=(1<<len(catalog))-1
data={}
for key in a["reads"].keys()|b["reads"].keys():
    if key.startswith("key:") and (not key[4:].isascii() or len(key)>104):continue
    mask=0
    for n in set(a["reads"].get(key,()))|set(b["reads"].get(key,())):
        # Some operating systems run a test the official catalog skips.
        if n.startswith("tests/") and n not in numbers:continue
        mask|=numbers.get(n,all_tests)
    data[key]=mask
Path(sys.argv[3]).write_bytes(gzip.compress(marshal.dumps((catalog, sorted(set(a["slots"])&set(b["slots"])),data)),mtime=0))
