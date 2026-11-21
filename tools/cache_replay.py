"""Unpack the committed observations at image build time."""
import compileall,gzip,json,lzma,marshal,pickle,sys
from pathlib import Path
r=Path(__file__).resolve().parents[1];cache=r/"cache";maps=r/"maps"
sys.path.insert(0,str(cache/"pinned"))
compileall.compile_dir(str(cache/"pinned"/"sqlglot"),quiet=2)
blocks=cache/"rules";blocks.mkdir(exist_ok=True)
ix=marshal.loads(gzip.decompress((maps/"query-rule-index.gz").read_bytes()))
with (maps/"query-rule-blocks.bin").open("rb") as stream:
    for start,size in sorted({(start,size) for start,size,_ in ix.values()}):
        stream.seek(start)
        # Individual inputs are fetched on demand by the charged selector.
        for i,raw in enumerate(marshal.loads(lzma.decompress(stream.read(size)))):
            (blocks/f"{start}-{i}").write_bytes(raw)
inputs=cache/"query-inputs";inputs.mkdir(exist_ok=True)
for item in (maps/"query-inputs").glob("*.gz"):
    (inputs/item.stem).write_bytes(gzip.decompress(item.read_bytes()))
for name in ("tpcds","tpch"):
    schema,rows=pickle.loads(gzip.decompress((maps/("tables-"+name+".gz")).read_bytes()))
    out=cache/name;out.mkdir(exist_ok=True)
    (out/"schema").write_bytes(pickle.dumps(schema,protocol=5))
    for table,data in rows.items():(out/("table-"+table)).write_bytes(pickle.dumps(data,protocol=5))
for name in ("query-traces","query-rule-index","inspection","conditions","predicate-observations","identities"):
    asset=maps/(name+".gz")
    if asset.is_file():(cache/name).write_bytes(gzip.decompress(asset.read_bytes()))

for name in ("order-shapes","sources","flags"):
    source=maps/(name+".json.gz")
    if source.is_file() and not (cache/name).exists():
        (cache/name).write_bytes(marshal.dumps(json.loads(gzip.decompress(source.read_bytes()))))
