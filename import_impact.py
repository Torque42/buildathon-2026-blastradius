"""Preserve tests affected by cold import failures and shared expression schemas."""
from functools import lru_cache
import importlib
import json
import marshal
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
if __name__ == "__main__":
    sys.path.insert(0,str(ROOT))

import isolated_imports
import schema_keys
from replay_edit import Overlay

@lru_cache(maxsize=1)
def profile():
    return marshal.loads((ROOT/"cache/import-impacts").read_bytes())


def schema_readers(selector, classes):
    catalog, rows = schema_keys.profile()
    if catalog != set(selector.NUM) or any(cls not in rows for cls in classes):
        return selector.ALL
    keep = set()
    for cls in classes:
        for context in rows[cls]:
            if context in selector.NUM:
                keep.add(selector.NUM[context])
            elif context.startswith(("scope:","test-import:")):
                file = context.split(":",1)[1]
                if file not in selector.BY_FILE:
                    return selector.ALL
                keep.update(selector.BY_FILE[file])
                if context.startswith("test-import:"):
                    keep.update(selector.dependent_test_numbers(file))
            elif context.startswith("import:"):
                keep.update(isolated_imports.readers(selector, context[7:]))
            else:
                return selector.ALL
    return keep


def include(selector, text, selected):
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return selected
    patch = patches[0]
    path = patch["old"]
    if not path or path != patch["new"] or path not in selector.SRC or not path.startswith("sqlglot/"):
        return selected
    lines, tables, classes = profile()
    if path not in lines and path not in tables:
        return selected
    source = selector.SRC[path]
    new, removed, added = selector.apply(source,patch["hunks"])
    touched = set(removed) | set(added)
    for first,last,name in tables.get(path,()):
        if classes.get(name) and any(first <= n <= last for n in touched):
            selected = selected | schema_readers(selector,classes[name])
    # Replay only scalar changes to existing code. Unsupported edits keep the
    # enclosing selector's fallbacks and are never executed by this check.
    if len(removed) != 1 or len(added) != 1 or not lines.get(path,{}).get(removed[0]):
        return selected
    index = removed[0] - 1
    before, after = source.splitlines(), new.splitlines()
    if len(before)!=len(after) or before[:index]!=after[:index] or before[index+1:]!=after[index+1:]:
        return selected
    if not selector.simple_token_change(before[index],after[index]):
        return selected
    modules = sorted(lines[path][removed[0]])
    try:
        run = subprocess.run([sys.executable,"-I",str(ROOT/"import_impact.py")],
                             input=json.dumps([path,new,modules]).encode(),capture_output=True,timeout=1.8)
        failed = json.loads(run.stdout) if run.returncode==0 else modules
    except (OSError, ValueError, subprocess.TimeoutExpired):
        failed = modules
    for module in failed:
        filename = selector.module_path(module)
        if not filename:
            return set(selector.ALL)
        selected = selected | isolated_imports.readers(selector,filename)
    return selected


if __name__ == "__main__":
    path, source, modules = json.load(sys.stdin)
    pin = ROOT/"cache/pinned"
    sys.path.insert(0,str(pin))
    sys.meta_path.insert(0,Overlay(path,source,pin))
    failures = []
    for module in modules:
        try:
            importlib.import_module(module)
        except Exception:
            failures.append(module)
    print(json.dumps(failures))
