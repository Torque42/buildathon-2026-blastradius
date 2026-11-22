"""Retain callers of dispatch callbacks whose key or implementation is edited or removed."""
import ast
from functools import lru_cache
import marshal
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def collect(path, tree):
    index = {}
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if (not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name)
                    or stmt.targets[0].id not in {"TRANSFORMS","FUNCTIONS"} or not isinstance(stmt.value, ast.Dict)):
                continue
            for key, value in zip(stmt.value.keys, stmt.value.values):
                if key is None or not isinstance(value, (ast.Lambda, ast.Call)):
                    continue
                site = f"{path}:{value.lineno}:{value.col_offset}"
                for line in range(key.lineno, value.end_lineno + 1):
                    index.setdefault(line, []).append(site)
    return index


@lru_cache(1)
def cached():
    path = ROOT/"cache/callback-entries"
    return marshal.loads(path.read_bytes()) if path.is_file() else None


@lru_cache(16)
def index(selector, path):
    data = cached()
    return data.get(path, {}) if data is not None else collect(path, selector.source_tree(path))


def include(selector, text, selected):
    extra = set()
    for patch in selector.parse_patch(text):
        path = patch["old"]
        if not path or path not in selector.SRC or not path.startswith(("sqlglot/generators/", "sqlglot/parsers/")):
            continue
        lines = index(selector, path)
        sites = set()
        for h in patch["hunks"]:
            pos = h["os"]
            for raw in h["lines"]:
                if raw.startswith("-"):
                    sites.update(lines.get(pos, ()))
                if raw.startswith(("-", " ")):
                    pos += 1
        if not sites:
            continue
        catalog, covered, readers = selector.dispatch_map()
        if not catalog:
            continue
        for site in sites & covered:
            for name in readers.get(site, ()):
                if name in selector.NUM:
                    extra.add(selector.NUM[name])
                elif name.startswith("scope:") and name[6:] in selector.BY_FILE:
                    extra.update(selector.BY_FILE[name[6:]])
                else:
                    return selector.ALL
    return selected | extra
