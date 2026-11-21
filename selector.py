"""Blast radius selector: map each patch to tests with a per-test coverage map
built at the pinned sqlglot commit, then split them over workers.

Reads /data (or argv[1]), prints one JSON line per commit, logs to stderr."""

import ast
import gzip
import io
import json
import os
import re
import sys
import tarfile
import tokenize
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/data")
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- loading
M = json.load(gzip.open(HERE / "maps" / "maps.json.gz", "rt"))
SETS = M["sets"]
MAP_TESTS = M["tests"]
SRC = {}
with tarfile.open(HERE / "maps" / "src.tar.xz") as tf:
    for m in tf.getmembers():
        if m.isfile():
            SRC[m.name] = tf.extractfile(m).read().decode("utf-8")

TJ = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
NUM = {t["id"]: t["number"] for t in TJ}
DUR = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in TJ}
BY_FILE = {}
for t in TJ:
    BY_FILE.setdefault(t["file"], []).append(t["number"])
ALL = set(DUR)
# map index -> number (tests unknown to the map are dropped from map sets)
IX2NUM = [NUM.get(t.split("[")[0], NUM.get(t)) for t in MAP_TESTS]


def tests_of(setidx):
    if setidx is None:
        return set()
    return {IX2NUM[i] for i in SETS[setidx] if IX2NUM[i] is not None}


# tests in tests.json that the coverage map never saw: always treat as unknown
UNMAPPED = ALL - {n for n in IX2NUM if n is not None}

pool_map_path = HERE / "maps" / "pool.json.gz"
if pool_map_path.is_file():
    with gzip.open(pool_map_path, "rt") as stream:
        POOL_MAP = json.load(stream)
else:
    POOL_MAP = {"tests": [], "lines": {}, "attributes": {}}
POOL_NUMBERS = [NUM.get(node_id) for node_id in POOL_MAP["tests"]]


@lru_cache(maxsize=2048)
def tests_from_pool_mask(mask):
    return frozenset(
        test for bit, test in enumerate(POOL_NUMBERS)
        if test is not None and mask & (1 << bit)
    )


# ---------------------------------------------------------------- patch parsing
def parse_patch(text):
    files = []
    cur = None
    for line in text.splitlines():
        if line.startswith("diff --git"):
            cur = {"old": None, "new": None, "hunks": []}
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("--- ") and not cur["hunks"]:
            p = line[4:].strip()
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ ") and not cur["hunks"]:
            p = line[4:].strip()
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            cur["hunks"].append({"os": int(m[1]), "ns": int(m[3]), "lines": []})
        elif cur["hunks"] and line[:1] in (" ", "+", "-", "\\", ""):
            if line.startswith("\\"):
                continue
            cur["hunks"][-1]["lines"].append(line if line else " ")
    for f in files:
        if f["old"] is None and f["new"] is None:
            m = re.match(r"diff --git a/(\S+) b/(\S+)", "")
    return files


def apply(old, hunks):
    """Return new text, list of changed old line numbers, list of old insertion points."""
    ol = old.split("\n") if old is not None else []
    out = []
    pos = 0  # 0-based index into ol
    removed, inserted = [], []
    for h in hunks:
        start = max(h["os"] - 1, 0) if h["os"] else 0
        out.extend(ol[pos:start])
        pos = start
        for l in h["lines"]:
            tag, body = l[0], l[1:]
            if tag == " ":
                out.append(ol[pos] if pos < len(ol) else body)
                pos += 1
            elif tag == "-":
                removed.append(pos + 1)
                pos += 1
            elif tag == "+":
                out.append(body)
                inserted.append(pos + 1)  # inserted before old line pos+1
    out.extend(ol[pos:])
    return "\n".join(out), removed, inserted


# ---------------------------------------------------------------- AST helpers
def strip_doc(tree):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            b = n.body
            if b and isinstance(b[0], ast.Expr) and isinstance(getattr(b[0], "value", None), ast.Constant) and isinstance(b[0].value.value, str):
                n.body = b[1:] or [ast.Pass()]
    return tree


def same_ast(a, b):
    try:
        return ast.dump(ast.parse(a)) == ast.dump(ast.parse(b))
    except SyntaxError:
        return False


class Locator:
    """For a source, find for a line its innermost statement and function context."""

    def __init__(self, src):
        self.tree = ast.parse(src)
        self.stmts = []  # (start, end, node, parents)
        self._walk(self.tree, [])

    def _walk(self, node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                self.stmts.append((child.lineno, child.end_lineno, child, parents))
            self._walk(child, parents + [child] if isinstance(child, (ast.stmt, ast.Lambda)) else parents)

    def innermost(self, line):
        best = None
        for s, e, n, p in self.stmts:
            if s <= line <= e and (best is None or (e - s) <= (best[1] - best[0])):
                best = (s, e, n, p)
        return best

    def lambdas_at(self, line):
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.Lambda) and n.lineno <= line <= n.end_lineno]


def header_end(n):
    body = getattr(n, "body", None)
    if isinstance(body, list) and body and isinstance(body[0], ast.AST):
        return body[0].lineno - 1
    return n.end_lineno


def names_in(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and len(n.value) < 60:
            out.add("str:" + n.value)
    return out


# ---------------------------------------------------------------- per-file analysis
def file_lines(path):
    return M["lines"].get(path, {})


def cov_range(path, a, b):
    fl = file_lines(path)
    pool_lines = POOL_MAP["lines"].get(path, {})
    mask = 0
    s = set()
    for ln in range(a, b + 1):
        key = str(ln)
        mask |= pool_lines.get(key, 0)
        k = fl.get(key)
        if k is not None:
            s |= tests_of(k)
    s.update(tests_from_pool_mask(mask))
    return s


def users(path):
    return tests_of(M["users"].get(path))


def importers_all(path):
    """Everything that may import this module (used when its import breaks)."""
    if path in M["collect"]:
        return set(ALL)
    return users(path) | UNMAPPED


def pool_attribute_readers(path, parents, names):
    module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    owner = ".".join(parent.name for parent in parents if isinstance(parent, ast.ClassDef))
    mask = 0
    for name in names:
        mask |= POOL_MAP["attributes"].get(f"{module}:{owner}:{name}", 0)
    return tests_from_pool_mask(mask)


def in_function(parents):
    for p in reversed(parents):
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return p
    return None


def stmt_tests(path, loc, line, why):
    """Tests that a change touching old `line` of a sqlglot module can affect."""
    hit = loc.innermost(line)
    if hit is None:
        return users(path), "module-other"
    s, e, n, parents = hit
    fn = in_function(parents)
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and line <= header_end(n):
        # signature, decorator or defaults
        if fn is None:
            # method/function defined at class/module level: body coverage + callers by name
            return cov_range(path, n.lineno, n.end_lineno) | (tests_of(M["names"].get(n.name)) & users(path)), "def-header"
        return cov_range(path, n.lineno, n.end_lineno), "def-header"
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return cov_range(path, n.lineno, n.end_lineno), "def-body-gap"
    if fn is not None:
        if isinstance(n, (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.AsyncFor, ast.AsyncWith)) and line <= header_end(n):
            return cov_range(path, s, header_end(n)) | cov_range(path, s, s), "compound-header"
        if isinstance(n, (ast.If, ast.For, ast.While, ast.With, ast.Try, ast.ClassDef, ast.FunctionDef)):
            # e.g. else: line
            return cov_range(path, s, e), "compound-body"
        return cov_range(path, s, e), "stmt"
    # class body / module level statement
    lam = loc.lambdas_at(line)
    t = set()
    for l in lam:
        t |= cov_range(path, l.lineno, l.end_lineno)
    if isinstance(n, ast.ClassDef) and line <= header_end(n):
        # class header (bases/decorators): all users of the class
        return cov_range(path, n.lineno, n.end_lineno) | users(path), "class-header"
    if isinstance(n, (ast.Import, ast.ImportFrom)):
        return importers_all(path), "import"
    nm = set()
    if isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        tg = n.targets if isinstance(n, ast.Assign) else [n.target]
        for x in tg:
            nm |= names_in(x)
    else:
        nm |= names_in(n)
    for x in nm:
        t |= tests_of(M["names"].get(x))
    u = users(path)
    # Attribute reads identify the defining class or module, so they do not
    # require a lexical match against other classes that share a field name.
    lexical = t & u if u else t
    return lexical | pool_attribute_readers(path, parents, nm), "module-level:" + ",".join(sorted(nm))[:60]


def only_comments(old, new, removed, inserted):
    """True if the patch only touches comment/blank lines outside strings."""
    def comment_lines(src):
        res = set()
        code = set()
        try:
            for tok in tokenize.generate_tokens(io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT:
                    res.add(tok.start[0])
                elif tok.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                    for l in range(tok.start[0], tok.end[0] + 1):
                        code.add(l)
        except Exception:
            return None, None
        return res, code
    return same_ast(old, new)


def analyze_py_source(path, f, reasons):
    old = SRC.get(path)
    if f["old"] is None:
        # new module: only matters if something imports it; nothing at the pin does
        reasons.append("new-file")
        return set()
    if old is None:
        reasons.append("unknown-file")
        return set(ALL)
    if f["new"] is None:
        reasons.append("deleted")
        return importers_all(path)
    new, removed, inserted = apply(old, f["hunks"])
    try:
        compile(new, path, "exec", dont_inherit=True)
    except SyntaxError:
        reasons.append("syntax-error")
        return importers_all(path)
    if same_ast(old, new):
        if new.split("\n")[:2] != old.split("\n")[:2]:
            reasons.append("header-lines")  # coding cookie territory
            return importers_all(path)
        reasons.append("comment-only")
        return set()
    loc = Locator(old)
    sel = set()
    for l in removed:
        t, why = stmt_tests(path, loc, l, "removed")
        sel |= t
        reasons.append(why)
    for l in inserted:
        if l in removed or (l - 1) in removed:
            continue
        # pure insertion before old line l: neighbours decide
        for nb in (l - 1, l):
            if nb < 1:
                continue
            # skip blank / comment neighbours by walking outwards
            t, why = stmt_tests(path, loc, nb, "insert")
            sel |= t
            reasons.append("ins-" + why)
    return sel


def test_file_tests(path, f, reasons):
    """Changes to a tests/*.py file."""
    in_file = set(BY_FILE.get(path, []))
    old = SRC.get(path)
    if old is None or f["new"] is None or f["old"] is None:
        reasons.append("testfile-whole")
        return in_file if path != "tests/helpers.py" else set(ALL)
    new, removed, inserted = apply(old, f["hunks"])
    try:
        compile(new, path, "exec", dont_inherit=True)
    except SyntaxError:
        reasons.append("testfile-syntax")
        return set(ALL) if path in ("tests/helpers.py", "tests/__init__.py") else in_file
    if same_ast(old, new):
        reasons.append("testfile-comment")
        return set()
    if path in ("tests/helpers.py", "tests/__init__.py") or not in_file:
        reasons.append("test-helper")
        return set(ALL)
    loc = Locator(old)
    sel = set()
    pref = path + "::"
    for l in removed + inserted:
        hit = loc.innermost(l) or loc.innermost(l - 1)
        meth = None
        cls = None
        if hit:
            for p in hit[3] + [hit[2]]:
                if isinstance(p, ast.ClassDef):
                    cls = p.name
                elif isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)) and cls and meth is None:
                    meth = p.name
        if cls and meth and meth.startswith("test"):
            ids = [n for i, n in NUM.items() if i.startswith(pref) and i.split("::")[1:3] == [cls, meth]]
            if ids:
                sel |= set(ids)
                reasons.append("test-method")
                continue
        if cls:
            ids = {n for i, n in NUM.items() if i.startswith(pref + cls + "::")}
            sel |= ids or in_file
        else:
            sel |= in_file
        reasons.append("test-class-or-module")
    return sel


def pool_tests():
    """Recover test callers of pool dispatch helpers in the pinned test sources."""
    result = set()
    for path in BY_FILE:
        source = SRC.get(path, "")
        if "ProcessPoolExecutor" not in source:
            continue
        tree = ast.parse(source)
        for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
            callers = {}
            dispatched = set()
            for method in cls.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                calls = [n.func for n in ast.walk(method) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)]
                if any(call.attr in {"submit", "map"} for call in calls):
                    dispatched.add(method.name)
                callers[method.name] = {call.attr for call in calls
                                        if isinstance(call.value, ast.Name)
                                        and call.value.id in {"self", "cls"}}
            while True:
                found = {name for name, uses in callers.items() if uses & dispatched}
                if found <= dispatched:
                    break
                dispatched.update(found)
            for name in dispatched:
                number = NUM.get(f"{path}::{cls.name}::{name}")
                if number is not None:
                    result.add(number)
    return result


UNTRACED_POOL_TESTS = pool_tests().difference(POOL_NUMBERS)


def analyze(text):
    reasons = []
    sel = set()
    for f in parse_patch(text):
        path = f["new"] or f["old"]
        if path.startswith("sqlglot/") and path.endswith(".py"):
            file_reasons = []
            got = analyze_py_source(path, f, file_reasons)
            reasons.extend(file_reasons)
            if file_reasons != ["comment-only"]:
                got |= UNTRACED_POOL_TESTS | UNMAPPED
            sel |= got
            if path.startswith("sqlglot/typing/") and "comment-only" not in file_reasons:
                sel |= set(BY_FILE.get("tests/test_typing.py", []))
        elif path.startswith("tests/") and path.endswith(".py"):
            sel |= test_file_tests(path, f, reasons)
        elif path.startswith("tests/"):
            k = M["opens"].get(path) if f["old"] else None
            if k is not None:
                sel |= tests_of(k)
                reasons.append("fixture")
            else:
                # unseen data file: tests whose file mentions its name
                base = os.path.basename(path)
                stem = os.path.splitext(base)[0]
                hits = {n for t in TJ for n in [t["number"]] if stem in SRC.get(t["file"], "")}
                reasons.append("fixture-unknown")
                sel |= hits
        elif path.startswith("sqlglotc/"):
            sel |= set(BY_FILE.get("tests/test_sqlglotc.py", []))
            reasons.append("sqlglotc")
        elif path.endswith(".py"):
            reasons.append("other-py")
            sel |= set(ALL)
        else:
            reasons.append("other:" + path)
    return sel & ALL, reasons


# ---------------------------------------------------------------- splitting
def improve_split(items, count, lower, best_peak, budget):
    """Find a smaller peak by branch and bound, up to budget assignment nodes."""
    loads = [0] * count
    bins = [[] for _ in loads]
    result = None

    def search(pos):
        nonlocal best_peak, result, budget
        budget -= 1
        if pos == len(items):
            peak = max(loads)
            if peak < best_peak:
                best_peak = peak
                result = [worker.copy() for worker in bins]
            return

        test = items[pos]
        duration = DUR[test]
        seen_loads = set()
        for i in sorted(range(count), key=loads.__getitem__):
            if budget <= 0 or best_peak == lower:
                break
            load = loads[i]
            # Equal loads have interchangeable completions, even with different tests.
            if load in seen_loads or load + duration >= best_peak:
                continue
            seen_loads.add(load)
            loads[i] += duration
            bins[i].append(test)
            search(pos + 1)
            bins[i].pop()
            loads[i] = load

    search(0)
    return result, best_peak, budget


def split(sel):
    if not sel:
        return []
    items = sorted(sel, key=DUR.__getitem__, reverse=True)
    total = sum(DUR[n] for n in items)
    longest = DUR[items[0]]
    candidates = []
    for count in range(1, min(8, len(items)) + 1):
        peak_bound = max(longest, (total + count - 1) // count)
        candidates.append((peak_bound + WORKER_MS * count, count))
    candidates.sort()

    best_cost = total + WORKER_MS
    best = [items]
    for bound, count in candidates:
        if bound >= best_cost:
            break
        loads = [0] * count
        bins = [[] for _ in loads]
        for test in items:
            i = loads.index(min(loads))
            loads[i] += DUR[test]
            bins[i].append(test)
        cost = max(loads) + WORKER_MS * count
        if cost < best_cost:
            best_cost, best = cost, bins

    # The bound often proves optimality immediately. Cap work on unresolved
    # patches because the competition charges CPU time as well as worker time.
    if len(items) <= 48:
        budget = 4000
        for bound, count in candidates:
            if bound >= best_cost or budget <= 0:
                break
            fee = WORKER_MS * count
            bins, peak, budget = improve_split(items, count, bound - fee, best_cost - fee, budget)
            if bins is not None:
                best_cost, best = peak + fee, bins
    return best


def main():
    total = 0
    for p in sorted((DATA / "commits").glob("*.patch")):
        try:
            sel, reasons = analyze(p.read_text("utf-8", errors="replace"))
        except Exception as e:  # never crash: run everything
            sel, reasons = set(ALL), ["error:" + repr(e)[:80]]
        workers = split(sel) if sel else []
        cost = (max(sum(DUR[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total += cost
        log(f"{p.stem}: {len(sel)} tests, cost {cost/1000:.1f}s, {sorted(set(reasons))[:6]}")
        print(json.dumps({"commit": p.stem, "workers": workers}, separators=(",", ":")))
    log(f"estimated simulated total {total/1000:.1f}s")


if __name__ == "__main__":
    main()
