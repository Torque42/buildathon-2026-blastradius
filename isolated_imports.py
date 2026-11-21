"""Keep tests that read or import an edited module in a pristine, cold pytest run."""
import gzip
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def profile():
    data = json.loads(gzip.decompress((Path(__file__).parent/"maps/isolated-imports.json.gz").read_bytes()))
    return set(data["catalog"]), {name: frozenset(paths) for name, paths in data["profiles"].items()}


@lru_cache(maxsize=256)
def readers(selector, path):
    known, profiles = profile()
    keep = set(selector.tests_of(selector.M["opens"].get(path)))
    for number in selector.ALL:
        name = selector.TEST_FILE_ID[number]
        file = selector.TEST_FILE[number]
        # A known missing dependency in a cold single-node run also covers collection.
        paths = profiles.get(name, profiles.get(file))
        if name not in known or paths is None or path in paths:
            keep.add(number)
    keep.update(selector.users(path))
    return keep


def refine(selector, text, selected):
    if not selected:
        return selected
    patches = selector.parse_patch(text)
    paths = {patch["old"] for patch in patches}
    if not patches or any(patch["old"] != patch["new"] for patch in patches):
        return selected
    if any(not path or not path.startswith("sqlglot/") or not path.endswith(".py") or path not in selector.SRC for path in paths):
        return selected
    return selected.intersection(set.union(*(readers(selector, path) for path in paths)))
