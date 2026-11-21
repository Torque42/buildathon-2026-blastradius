"""Record pristine tokenizer keyword reads and generator reserved-word checks."""
import atexit
import builtins
import gzip
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import sys

active = os.environ.get("BR_FLAG_TEST", "")
loading = 0
reads = {}
owners = {}
initialized = set()


def record(site, key):
    context = "" if loading else active
    if isinstance(key, str):
        reads.setdefault((site, context), set()).add(key)
    else:
        reads.setdefault((site, context), set()).add("*")
    return key


class KeywordLookups:
    def __init__(self, table):
        self.table = table

    def __contains__(self, key):
        return record("token", key) in self.table

    def __getitem__(self, key):
        return self.table[record("token", key)]

    def get(self, key, default=None):
        return self.table.get(record("token", key), default)


def reserved(generator, word):
    cls = type(generator)
    if cls not in owners:
        owner = next(base for base in cls.__mro__ if "RESERVED_KEYWORDS" in vars(base))
        owners[cls] = owner.__module__ + ":" + owner.__qualname__
    return record(owners[cls], word)


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        global loading
        loading += 1
        try:
            if module.__name__ in ("sqlglot.tokenizer_core", "sqlglot.generator"):
                source = self.get_data(self.path).decode("utf-8")
                if module.__name__ == "sqlglot.tokenizer_core":
                    before, after = "self.keywords = keywords", "self.keywords = _lexical_table(keywords)"
                else:
                    before, after = "lower in self.RESERVED_KEYWORDS", "_lexical_reserved(self, lower) in self.RESERVED_KEYWORDS"
                if source.count(before) != 1:
                    raise ValueError(f"Unrecognized {module.__name__} on the pin")
                exec(compile(source.replace(before, after), self.path, "exec", dont_inherit=True), module.__dict__)
                initialized.add(module.__name__)
            else:
                super().exec_module(module)
        finally:
            loading -= 1


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "sqlglot" or fullname.startswith("sqlglot."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
                return spec


def save():
    rows = [(site, context, sorted(words)) for (site, context), words in reads.items()]
    payload = json.dumps([sorted(initialized), rows], separators=(",", ":")).encode()
    (Path(os.environ["BR_FLAG_DIR"]) / f"{os.getpid()}.json.gz").write_bytes(gzip.compress(payload,mtime=0))


if "BR_FLAG_DIR" in os.environ:
    builtins._lexical_table = KeywordLookups
    builtins._lexical_reserved = reserved
    sys.meta_path.insert(0, Finder())
    atexit.register(save)
