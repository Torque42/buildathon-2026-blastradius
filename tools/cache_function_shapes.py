"""Build pristine Python AST and bytecode checkpoints with the deployed interpreter."""

import hashlib
import marshal
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import function_changes

sources = marshal.loads((root / "cache/sources").read_bytes())
for path, source in sources.items():
    if path.startswith("sqlglot/") and path.endswith(".py"):
        item = function_changes.SHAPES / (path + ".marshal")
        item.parent.mkdir(parents=True, exist_ok=True)
        result = function_changes.make_baseline(path, source)
        item.write_bytes(marshal.dumps((hashlib.sha256(source.encode()).digest(), sys.version_info[:2], result)))
