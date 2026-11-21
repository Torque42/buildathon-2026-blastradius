"""Retain users of compiled SQL identifiers across pristine environments."""
import gzip
import json
import sys
from pathlib import Path

destination, *paths = map(Path, sys.argv[1:])
records = [json.loads(gzip.decompress(path.read_bytes())) for path in paths]
catalog = records[0]["catalog"]
readers = {}
for record in records:
    if record["catalog"] != catalog:
        raise ValueError("inconsistent test catalog")
    for key, ids in record["readers"].items():
        readers.setdefault(key, set()).update(ids)
output = {"catalog": catalog, "readers": {key: sorted(ids) for key,ids in readers.items()}}
destination.write_bytes(gzip.compress(json.dumps(output,separators=(",",":")).encode(),mtime=0))
