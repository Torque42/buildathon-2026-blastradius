"""Map cold profiles to catalog IDs, using both platforms for every exclusion."""
import gzip
import json
import marshal
import sys
from pathlib import Path

catalog_path, destination, *paths = map(Path, sys.argv[1:])
tests = json.loads(catalog_path.read_bytes())["tests"]
names = [record["id"] for record in tests]
known = (1 << len(names)) - 1
combined = {}
for path in paths:
    version, scopes, locations = marshal.loads(gzip.decompress(path.read_bytes()))
    if tuple(version) != tuple(sys.version_info[:2]):
        raise ValueError("compiler mismatch")
    numbers = {name: i for i, name in enumerate(scopes)}
    expansion = {}
    for i, record in enumerate(tests):
        index = numbers.get(record["id"], numbers.get(record["file"]))
        if index is None:
            known &= ~(1 << i)
        else:
            expansion[index] = expansion.get(index, 0) | (1 << i)
    memo = {}
    def expand(bits):
        if bits in memo:
            return memo[bits]
        result = 0
        while_bits = bits
        while while_bits:
            low = while_bits & -while_bits
            result |= expansion.get(low.bit_length()-1, 0)
            while_bits ^= low
        memo[bits] = result
        return result
    for file, members in locations.items():
        for member, groups in members.items():
            dest = combined.setdefault(file, {}).setdefault(member, {})
            for raw, scopes_mask in groups.items():
                mapped = expand(scopes_mask)
                if mapped:
                    mask = raw | 1
                    dest[mask] = dest.get(mask, 0) | mapped
out = (version, names, known, combined)
destination.write_bytes(gzip.compress(marshal.dumps(out), mtime=0))
print("known", known.bit_count(), "bytes", destination.stat().st_size)
