import ast
import gzip
import json
import tarfile
from collections import defaultdict
from pathlib import Path

maps = Path(__file__).resolve().parents[1] / "maps"
bases = set()
writes = defaultdict(set)
sources = {}
with tarfile.open(maps / "src.tar.xz") as archive:
    for entry in archive:
        if not entry.isfile():
            continue
        sources[entry.name] = archive.extractfile(entry).read().decode("utf-8")
        if not entry.name.endswith(".py"):
            continue
        tree = ast.parse(sources[entry.name])
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    bases.update(item.id for item in ast.walk(base) if isinstance(item, ast.Name))
                    bases.update(item.attr for item in ast.walk(base) if isinstance(item, ast.Attribute))
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                    if isinstance(target, (ast.Name, ast.Attribute)):
                        writes[getattr(target, "attr", None) or target.id].add(entry.name)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"setattr", "delattr"}:
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        writes[arg.value].add(entry.name)
(maps / "source-index.json.gz").write_bytes(gzip.compress(json.dumps([sorted(bases), {field: sorted(paths) for field, paths in writes.items()}]).encode(), mtime=0))
(maps / "sources.json.gz").write_bytes(gzip.compress(json.dumps(sources, ensure_ascii=False).encode(), mtime=0))
