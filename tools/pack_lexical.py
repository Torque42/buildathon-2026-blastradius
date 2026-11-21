"""Union complete platform traces into an inverted index of test contexts."""
import gzip
import json
import marshal
from pathlib import Path
import sys

left, right, output = map(Path, sys.argv[1:])
parts = [json.loads(gzip.decompress(path.read_bytes())) for path in (left, right)]
if parts[0]["catalog"] != parts[1]["catalog"]:
    raise ValueError("Platform catalogs disagree")
contexts = sorted({context for part in parts for groups in part["sites"].values() for context in groups})
by_context = {context:1 << i for i,context in enumerate(contexts)}
sites = {}
for part in parts:
    for name, groups in part["sites"].items():
        index = sites.setdefault(name, {})
        for context, words in groups.items():
            bit = by_context[context]
            for word in words:
                if word == "*" or word.isascii() and word.isidentifier():
                    index[word] = index.get(word, 0) | bit
output.write_bytes(gzip.compress(marshal.dumps((parts[0]["catalog"],contexts,sites)),mtime=0))
print(output.stat().st_size, len(contexts), len(sites))
