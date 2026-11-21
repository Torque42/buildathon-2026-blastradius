"""Find the first bytecodes where old and new function executions can diverge."""
import dis
import difflib
import gzip
import hashlib
import marshal
import sys
from functools import lru_cache
from pathlib import Path
from types import CodeType
from replay_edit import preserve_scope

ROOT = Path(__file__).resolve().parent

@lru_cache(1)
def observations():
    cached = ROOT / "cache/instruction-observations"
    raw = cached.read_bytes() if cached.is_file() else gzip.decompress((ROOT/"maps/instruction-observations.gz").read_bytes())
    version, names, known, files = marshal.loads(raw)
    return version, {name:1 << i for i,name in enumerate(names)}, known, files

def lineage(code, prefix=()):
    yield prefix, code
    children = [v for v in code.co_consts if isinstance(v, CodeType)]
    for i, child in enumerate(children):
        yield from lineage(child, (*prefix, i, child.co_name))

@lru_cache(maxsize=24)
def original(path, content):
    return dict(lineage(compile(content, path, "exec", dont_inherit=True)))

def token(ins):
    if ins.opcode in dis.hasjump:
        return ins.opname, "jump"
    if ins.opcode in dis.hasconst:
        value = ins.argval
        if isinstance(value, CodeType):
            return ins.opname, ("body", value.co_name, value.co_qualname)
        return ins.opname, ("constant", marshal.dumps(value))
    if ins.opcode in dis.hasname:
        return ins.opname, ins.argval, ins.argrepr
    return ins.opname, ins.arg, ins.argrepr

def metadata(code):
    return (code.co_name, code.co_qualname, code.co_flags, code.co_argcount, code.co_kwonlyargcount,
            code.co_posonlyargcount, code.co_varnames, code.co_freevars, code.co_cellvars)

def offset_mask(before, after):
    # Replacing a function's metadata can affect code that never invokes it.
    if metadata(before) != metadata(after):
        return None
    if before.co_flags & 1:
        doc_a = before.co_consts[0] if before.co_consts and type(before.co_consts[0]) is str else None
        doc_b = after.co_consts[0] if after.co_consts and type(after.co_consts[0]) is str else None
        if doc_a != doc_b:
            return None
    left, right = list(dis.get_instructions(before)), list(dis.get_instructions(after))
    a, b = [token(v) for v in left], [token(v) for v in right]
    if a == b:
        pairs = dict(enumerate(range(len(a))))
    else:
        finder = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
        pairs = {i+delta:j+delta for i,j,size in finder.get_matching_blocks() for delta in range(size)}
    if before.co_exceptiontable or after.co_exceptiontable:
        # Offset shifts can move try regions even if the source change is tiny.
        if before.co_exceptiontable != after.co_exceptiontable or [x.offset for x in left] != [x.offset for x in right]:
            return None
    left_offsets = {op.offset:i for i,op in enumerate(left)}
    right_offsets = {op.offset:i for i,op in enumerate(right)}
    mask = 0 if pairs.get(0) == 0 else 1
    for i, op in enumerate(left):
        j = pairs.get(i)
        different = j is None
        if j is not None:
            if i + 1 < len(left) and pairs.get(i+1) != j+1:
                different = True
            if op.opcode in dis.hasjump:
                target = left_offsets.get(op.argval)
                if target is None or pairs.get(target, -1) != right_offsets.get(right[j].argval):
                    different = True
        if different:
            mask |= 1 << (op.offset // 2)
            if op.opname in ("RESUME", "RETURN_GENERATOR", "COPY_FREE_VARS", "MAKE_CELL"):
                mask |= 1
    return mask

def affected(selector, patch, details=None):
    path = patch["old"]
    if not path or path != patch["new"] or not path.startswith("sqlglot/") or path not in selector.SRC:
        return None
    if path in selector.profile()["source_reads"]:
        return None
    source = selector.SRC[path]
    new, removed, added = selector.apply(source, patch["hunks"])
    if not removed or not added or len(removed) + len(added) > 20 or source.count("\n") != new.count("\n"):
        return None
    try:
        aa = original(path, source)
        bb = dict(lineage(compile(new, path, "exec", dont_inherit=True)))
    except (SyntaxError, ValueError, OverflowError):
        return None
    if aa.keys() != bb.keys():
        return None
    version, names, known, files = observations()
    if tuple(sys.version_info[:2]) != tuple(version) or path not in files:
        return None
    records = files[path]
    impacted = 0
    inspected = 0
    count = 0
    for key, a in aa.items():
        b = bb[key]
        if a == b:
            continue
        # A compiled parent compares unequal when only a nested function changes.
        if metadata(a) == metadata(b) and a.co_code == b.co_code and a.co_names == b.co_names and a.co_exceptiontable == b.co_exceptiontable:
            def shallow(c):
                return tuple(("body", v.co_qualname) if isinstance(v, CodeType) else ("constant", marshal.dumps(v)) for v in c.co_consts)
            if shallow(a) == shallow(b):
                continue
        if not a.co_flags & 1 or a.co_firstlineno != b.co_firstlineno:
            return None
        count += 1
        if count > 8:
            return None
        wanted = offset_mask(a,b)
        if wanted is None:
            return None
        # Keep tests if their interpreter or import hook ran different bytecode.
        identifier = (a.co_qualname, a.co_firstlineno, hashlib.blake2s(a.co_code,digest_size=12).digest())
        if identifier not in records:
            return None
        if details is not None:
            details.append((identifier, wanted))
        for member, groups in records.items():
            if member[:2] != identifier[:2]:
                continue
            for visited, group in groups.items():
                if member != identifier or visited & wanted:
                    impacted |= group
        for _, _, line in a.co_lines():
            if line is not None:
                inspected |= 1 << line
    if not count:
        return None
    return path, impacted, inspected

@lru_cache(maxsize=4)
def single_impact(selector, text):
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return None, ()
    details = []
    impact = affected(selector, patches[0], details)
    return impact, tuple(details)

def refine(selector, text, selected):
    if not selected:
        return selected
    patches = selector.parse_patch(text)
    if not patches or len(patches) > 3:
        return selected
    edits = [single_impact(selector,text)[0]] if len(patches)==1 else [affected(selector, patch) for patch in patches]
    if any(edit is None for edit in edits):
        return selected
    version, names, known, _ = observations()
    unsafe = 0
    keep = set()
    for path, impacted, inspected in edits:
        unsafe |= impacted
        keep.update(selector.tests_of(selector.M["opens"].get(path)))
        keep.update(preserve_scope(selector,path,inspected,selected))
        keep.update(n for n, (raw, _) in selector.EXTRA.items() if path in raw)
    safe = known & ~unsafe
    return {n for n in selected if n in keep or not safe & names.get(selector.TEST_FILE_ID[n], 0)}
