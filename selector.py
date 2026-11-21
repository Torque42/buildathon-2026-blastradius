"""Blast radius selector: map each patch to tests with a per-test coverage map
built at the pinned sqlglot commit, then split them over workers.

Reads /data (or argv[1]), prints one JSON line per commit, logs to stderr."""

import ast
import builtins
import gzip
import io
import json
import marshal
import os
import re
import sys
import tokenize
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/data")
sys.path.insert(0,str(HERE))
import query_replay
import query_phases
import flag_readers
import identity_select
import predicate_select
import condition_select
import compound
import isolated_imports
import validation_inputs
import fixture_inputs
import observed_inputs
import instruction_impact
import parser_table_keys
import schema_keys
import registry_inputs
import default_inputs
import constructor_inputs
import lexical_inputs
import get_inputs
import replay_edit
WORKER_MS = 2000


def log(*a):
    print(*a, file=sys.stderr)


# ---------------------------------------------------------------- loading
def load_map(name):
    binary = HERE / "cache" / name
    if binary.is_file():
        with binary.open("rb") as stream:
            return marshal.loads(stream.read())
    return json.load(gzip.open(HERE / "maps" / (name + ".json.gz")))

M = load_map("maps")
SETS = M["sets"]
MAP_TESTS = M["tests"]
SRC = load_map("sources")

TJ = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
NUM = {t["id"]: t["number"] for t in TJ}
TEST_FILE_ID = {t["number"]:t["id"] for t in TJ}
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
    POOL_MAP = load_map("pool")
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
def diff_path(value):
    """Decode quoted git paths and strip an optional unified-diff timestamp."""
    value = value.rstrip().split("\t", 1)[0]
    if value.startswith('"'):
        value = os.fsdecode(ast.literal_eval("b" + value))
    return value


def parse_patch(text):
    files = []
    cur = None
    for line in text.splitlines():
        if line.startswith("diff --git"):
            cur = {"old": None, "new": None, "hunks": []}
            files.append(cur)
        elif cur is None:
            continue
        elif line.startswith("rename from "):
            cur["old"] = diff_path(line[len("rename from "):])
        elif line.startswith("rename to "):
            cur["new"] = diff_path(line[len("rename to "):])
        elif line.startswith("copy from "):
            cur["copy"] = True
        elif line.startswith("copy to "):
            cur["new"] = diff_path(line[len("copy to "):])
        elif line.startswith("--- ") and not cur["hunks"]:
            p = diff_path(line[4:])
            cur["old"] = None if p == "/dev/null" else p[2:] if p.startswith("a/") else p
        elif line.startswith("+++ ") and not cur["hunks"]:
            p = diff_path(line[4:])
            cur["new"] = None if p == "/dev/null" else p[2:] if p.startswith("b/") else p
        elif line.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            cur["hunks"].append({"os": int(m[1]), "ns": int(m[3]), "lines": []})
        elif cur["hunks"] and line[:1] in (" ", "+", "-", "\\", ""):
            if line.startswith("\\"):
                continue
            cur["hunks"][-1]["lines"].append(line if line else " ")
    changes = []
    for f in files:
        old, new = f["old"], f["new"]
        if f.get("copy"):
            changes.append({"old": None, "new": new, "hunks": []})
        elif old and new and old != new:
            changes.append({"old": old, "new": None, "hunks": []})
            changes.append({"old": None, "new": new, "hunks": []})
        else:
            changes.append(f)
    return changes


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


def equal_ast(left, right, changed_line=None):
    """Compare Python syntax without materializing strings for both trees."""
    pending = [(left, right)]
    while pending:
        a, b = pending.pop()
        if a is b:
            continue
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.AST):
            if changed_line and hasattr(a, "lineno") and (a.end_lineno < changed_line or a.lineno > changed_line) and a.lineno == b.lineno and a.end_lineno == b.end_lineno and a.col_offset == b.col_offset and a.end_col_offset == b.end_col_offset:
                continue
            pending.extend((value, getattr(b, name)) for name, value in ast.iter_fields(a))
        elif isinstance(a, list):
            if len(a) != len(b):
                return False
            pending.extend(zip(reversed(a), reversed(b)))
        elif isinstance(a, (float, complex)):
            if repr(a) != repr(b):
                return False
        elif a != b:
            return False
    return True


def same_ast(a, b):
    try:
        return equal_ast(ast.parse(a), ast.parse(b))
    except SyntaxError:
        return False


class Locator:
    """For a source, find for a line its innermost statement and function context."""

    def __init__(self, src, tree=None):
        self.tree = tree if tree is not None else ast.parse(src)
        self.stmts = []  # (start, end, node, parents)
        self.lambdas = []
        self._walk(self.tree, [])

    def _walk(self, node, parents):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Lambda):
                self.lambdas.append(child)
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
        return [n for n in self.lambdas if n.lineno <= line <= n.end_lineno]


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
    for scope, lines in FIXTURE_LINES.get(path, ()):
        if any(a <= line <= b for line in lines):
            s.update(scope_tests(scope))
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


@lru_cache(maxsize=1)
def attribute_profile():
    profile = load_map("observed-attributes")
    return [NUM.get(test) for test in profile["tests"]], profile["slots"]


def observed_attribute_tests(path, parents, names):
    owner = ".".join(node.name for node in parents if isinstance(node, ast.ClassDef))
    if not owner or not names:
        return set()
    numbers, slots = attribute_profile()
    module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    return {numbers[i] for name in names for i in slots.get(f"{module}:{owner}:{name}", ())
            if numbers[i] is not None}


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
    lexical.update(observed_attribute_tests(path, parents, nm))
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


def added_lines(hunks):
    """Return added line numbers in the new source."""
    changed = set()
    for hunk in hunks:
        lineno = hunk["ns"]
        for line in hunk["lines"]:
            if line.startswith("+"):
                changed.add(lineno)
            if not line.startswith("-"):
                lineno += 1
    return changed


def changed_encoding(old, new):
    """A coding cookie changes how Python interprets the source bytes on import."""
    old_header = old.split("\n", 2)[:2]
    new_header = new.split("\n", 2)[:2]
    if old_header == new_header:
        return False
    def encoding(lines):
        return tokenize.detect_encoding(io.BytesIO(("\n".join(lines) + "\n").encode()).readline)[0]
    return encoding(old_header) != encoding(new_header)


def eager_failure(tree, changed):
    """Detect direct import-time errors introduced into an eager expression."""
    if not changed:
        return False

    # Function bodies run later, but decorators and defaults run as soon as the
    # definition is executed. Class bodies, base expressions and decorators run
    # during class creation.
    scopes = [tree]
    expressions = []
    while scopes:
        for statement in scopes.pop().body:
            if isinstance(statement, ast.ClassDef):
                scopes.append(statement)
                expressions.extend(statement.decorator_list)
                expressions.extend(statement.bases)
                expressions.extend(keyword.value for keyword in statement.keywords)
            elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                expressions.extend(statement.decorator_list)
                expressions.extend(statement.args.defaults)
                expressions.extend(expr for expr in statement.args.kw_defaults if expr)
            elif isinstance(statement, ast.Raise) and statement.lineno in changed:
                return True
            elif isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr)):
                if statement.value is not None:
                    expressions.append(statement.value)
            elif isinstance(statement, ast.Assert):
                if statement.lineno in changed and isinstance(statement.test, ast.Constant):
                    if not statement.test.value:
                        return True

    loads = set()
    while expressions:
        expr = expressions.pop()
        if isinstance(expr, (ast.Lambda, ast.GeneratorExp)):
            continue
        if hasattr(expr, "lineno") and not any(expr.lineno <= line <= expr.end_lineno for line in changed):
            continue
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if isinstance(expr.right, ast.Constant) and isinstance(expr.right.value, (int, float, complex)):
                if expr.right.value == 0:
                    return True
        if isinstance(expr, ast.Name) and isinstance(expr.ctx, ast.Load):
            if expr.lineno in changed:
                loads.add(expr.id)
        else:
            expressions.extend(child for child in ast.iter_child_nodes(expr)
                               if hasattr(child, "lineno") or isinstance(child, ast.comprehension))
    if not loads:
        return False
    # Deliberately over-approximate bindings across scopes. Names present in any
    # scope do not trigger this fallback, and dynamic definitions are kept safe
    # by selecting potential users when an unresolved load is encountered.
    bindings = set(vars(builtins)) | {"__name__", "__file__", "__package__", "__spec__",
                                     "__loader__", "__cached__", "__builtins__", "__annotations__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bindings.add(node.id)
        elif isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.arg)):
            bindings.add(node.arg if isinstance(node, ast.arg) else node.name)
        elif isinstance(node, ast.alias):
            bindings.add(node.asname or node.name.partition(".")[0])
        elif isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            # A star import can define names absent from the syntax tree.
            return False
    return bool(loads - bindings)


def class_members(tree):
    """Index assignment targets and methods by their containing class."""
    classes = {}
    scopes = [("", tree)]
    while scopes:
        prefix, scope = scopes.pop()
        for statement in scope.body:
            if not isinstance(statement, ast.ClassDef):
                continue
            name = prefix + statement.name
            scopes.append((name + ".", statement))
            members = {}
            for member in statement.body:
                if isinstance(member, ast.Assign):
                    targets = member.targets
                elif isinstance(member, ast.AnnAssign) and member.value is not None:
                    targets = [member.target]
                elif isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    members[member.name] = member.lineno
                    continue
                else:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        members[target.id] = target.lineno
            classes[name] = members
    return classes


def added_class_members(old_tree, new_tree, changed):
    """An inserted member can change inherited behavior, independent of neighbors."""
    old_classes = class_members(old_tree)
    return {
        (owner, name)
        for owner, members in class_members(new_tree).items()
        if owner in old_classes
        for name, line in members.items()
        if line in changed and name not in old_classes[owner]
    }


@lru_cache(maxsize=1)
def receiver_map():
    path = HERE / "maps" / "override-methods.json.gz"
    if not path.is_file():
        return {}, frozenset(), []
    with gzip.open(path, "rt") as stream:
        profile = json.load(stream)
    return profile["receivers"], frozenset(profile["early"]), [NUM.get(test) for test in profile["tests"]]


def inherited_dispatch_tests(path, members):
    """Find old dispatches where a new member can shadow an inherited method."""
    receivers, early, numbers = receiver_map()
    module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    mask = 0
    for owner, name in members:
        key = f"{module}:{owner}"
        if f"{key}:{name}" in early:
            return set(ALL)
        mask |= receivers.get(key, {}).get(name, 0)
    return {number for index, number in enumerate(numbers)
            if number is not None and mask & (1 << index)}


def added_member_readers(path, old_source, members):
    # Tests can observe the new binding through a base implementation or an
    # inherited type in another module. The previous file's users cannot
    # represent every dispatch to the new member.
    observed = {name: tests_of(M["names"].get(name)) for _, name in members}
    for qualified, mask in POOL_MAP["attributes"].items():
        name = qualified.rsplit(":", 1)[-1]
        if name in observed:
            observed[name].update(tests_from_pool_mask(mask))
    selected = inherited_dispatch_tests(path, members).union(*observed.values())
    if any(not tests for tests in observed.values()):
        selected |= users(path) | cov_range(path, 1, old_source.count("\n") + 1)
    return selected


def module_path(module):
    path = module.replace(".", "/")
    for name in (path + ".py", path + "/__init__.py"):
        if name in SRC:
            return name
    return None


@lru_cache(maxsize=32)
def source_tree(path):
    return ast.parse(SRC[path], filename=path)


def imported_class(path, name):
    statements = list(source_tree(path).body)
    for statement in statements:
        if isinstance(statement, ast.If) and isinstance(statement.test, ast.Attribute):
            if statement.test.attr == "TYPE_CHECKING":
                statements.extend(statement.body)
        if isinstance(statement, ast.ClassDef) and statement.name == name:
            return path, statement
        if isinstance(statement, ast.ImportFrom) and statement.level == 0:
            for alias in statement.names:
                if (alias.asname or alias.name) == name and statement.module:
                    resolved = module_path(statement.module)
                    if resolved:
                        for node in source_tree(resolved).body:
                            if isinstance(node, ast.ClassDef) and node.name == alias.name:
                                return resolved, node
    return None


def simple_enum(path, name, pending=frozenset()):
    if (path, name) in pending:
        return False
    found = imported_class(path, name)
    if not found:
        return False
    filename, cls = found
    if cls.keywords or cls.decorator_list:
        return False
    # String-backed Enum members compare by their value, and the standard
    # Enum metaclass aliases members with the same value to one object.
    enum_imports = {
        alias.asname or alias.name
        for stmt in source_tree(filename).body
        if isinstance(stmt, ast.ImportFrom) and stmt.module == "enum" and not stmt.level
        for alias in stmt.names if alias.name == "Enum"
    }
    has_enum_base = False
    for base in cls.bases:
        if not isinstance(base, ast.Name):
            return False
        if base.id == "str":
            continue
        if base.id in enum_imports or simple_enum(filename, base.id, pending | {(path, name)}):
            has_enum_base = True
        else:
            return False
    return has_enum_base and all(
        isinstance(node, (ast.Assign, ast.Expr, ast.Pass))
        or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_generate_next_value_"
        for node in cls.body
    )


def references_enum(expr, enum_name):
    if isinstance(expr, ast.Attribute):
        return isinstance(expr.value, ast.Name) and expr.value.id == enum_name
    return (
        isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name)
        and expr.func.id == enum_name
    )


BASE_NAMES, WRITE_PATHS = load_map("source-index")
FACTS = load_map("facts") if (HERE / "cache/facts").is_file() else {}

def enum_attribute(field, enum_name):
    """Require every syntactically visible write to produce an enum member."""
    key = (field, enum_name)
    if key in FACTS.get("enum", {}):
        return FACTS["enum"][key]
    pending = [field]
    examined = set()
    while pending:
        name = pending.pop()
        if name in examined:
            continue
        examined.add(name)
        writes = 0
        for path in WRITE_PATHS.get(name, ()):
            tree = source_tree(path)
            for node in ast.walk(tree):
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    if not any(
                        isinstance(target, ast.Attribute) and target.attr == name
                        or isinstance(target, ast.Name) and target.id == name and name != field
                        for target in targets
                    ):
                        continue
                    if references_enum(node.value, enum_name):
                        writes += 1
                    elif (
                        isinstance(node.value, ast.Attribute)
                        and isinstance(node.value.value, ast.Name)
                        and node.value.value.id == "self"
                        and node.value.attr != name
                        and node.value.attr not in examined
                    ):
                        pending.append(node.value.attr)
                    else:
                        return False
                elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    if node.func.id in {"setattr", "delattr"} and any(
                        isinstance(arg, ast.Constant) and arg.value == name for arg in node.args
                    ):
                        return False
        if not writes:
            return False
    return True


def optional_identity_field(path, field, cls):
    """Follow an annotated object cache whose values have default equality."""
    annotation = None
    class_type = None
    for statement in ast.walk(cls):
        if not isinstance(statement, ast.AnnAssign):
            continue
        if not self_field(statement.target, field) or not isinstance(statement.annotation, ast.BinOp):
            continue
        union = statement.annotation
        if (
            isinstance(union.op, ast.BitOr) and isinstance(union.left, ast.Name)
            and isinstance(union.right, ast.Constant) and union.right.value is None
        ):
            annotation, class_type = statement, union.left.id
            break
    if not annotation:
        return False
    found = imported_class(path, class_type)
    if not found:
        return False
    _, value_cls = found
    if value_cls.bases or value_cls.keywords or value_cls.decorator_list:
        return False
    specialized = {"__eq__", "__ne__", "__new__", "__getattribute__"}
    if any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in specialized
        or isinstance(node, ast.Assign) and any(names_in(target) & specialized for target in node.targets)
        or isinstance(node, ast.AnnAssign) and names_in(node.target) & specialized
        for node in value_cls.body
    ):
        return False
    if class_type in BASE_NAMES:
        return False
    # The inferred field is either empty or assigned from the values of a
    # mapping whose constructor input is declared as dict[..., this type].
    mappings = set()
    for statement in cls.body:
        if not isinstance(statement, ast.FunctionDef) or statement.name != "__init__":
            continue
        typed = {
            parameter.arg for parameter in statement.args.args
            if isinstance(parameter.annotation, ast.Subscript)
            and isinstance(parameter.annotation.value, ast.Name)
            and parameter.annotation.value.id == "dict"
            and isinstance(parameter.annotation.slice, ast.Tuple)
            and len(parameter.annotation.slice.elts) == 2
            and isinstance(parameter.annotation.slice.elts[1], ast.Name)
            and parameter.annotation.slice.elts[1].id == class_type
        }
        for setter in statement.body:
            if isinstance(setter, ast.Assign) and isinstance(setter.value, ast.Name) and setter.value.id in typed:
                mappings.update(target.attr for target in setter.targets if self_field(target))
    for statement in ast.walk(cls):
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        if any(self_field(target, field) for target in targets):
            if isinstance(statement.value, ast.Constant) and statement.value.value is None:
                continue
            value = statement.value
            if not isinstance(value, ast.Subscript) or not isinstance(value.value, ast.Call):
                return False
            call = value.value
            if not isinstance(call.func, ast.Name) or call.func.id != "list" or len(call.args) != 1:
                return False
            args = call.args[0]
            if (
                not isinstance(args, ast.Call) or not isinstance(args.func, ast.Attribute)
                or args.func.attr != "values" or not self_field(args.func.value)
                or args.func.value.attr not in mappings or args.args or args.keywords
            ):
                return False
    return bool(mappings)


def self_field(node, name=None):
    return (
        isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "self" and (name is None or node.attr == name)
    )


def equivalent_comparison(path, old, new_tree, removed, inserted):
    """Recognize a single comparison edit justified by pinned declarations."""
    if len(removed) != 1 or len(inserted) != 1:
        return False
    line = removed[0]
    original_line = old.splitlines()[line - 1]
    if " is " not in original_line:
        return False
    old_tree = ast.parse(old)
    operators = {ast.Is: ast.Eq, ast.IsNot: ast.NotEq}
    for original, updated in zip(ast.walk(old_tree), ast.walk(new_tree)):
        if not isinstance(original, ast.Compare) or not isinstance(updated, ast.Compare):
            continue
        if original.lineno != line or len(original.ops) != 1 or len(updated.ops) != 1:
            continue
        if operators.get(type(original.ops[0])) is not type(updated.ops[0]):
            continue
        original.ops = updated.ops
        if not equal_ast(old_tree, new_tree):
            return False
        a, b = original.left, original.comparators[0]
        left, right = (a, b) if self_field(a) else (b, a)
        if not self_field(left):
            return False
        if isinstance(right, ast.Attribute) and isinstance(right.value, ast.Name):
            name = right.value.id
            if not simple_enum(path, name):
                return False
            _, enum = imported_class(path, name)
            member = any(
                isinstance(stmt, ast.Assign)
                and any(isinstance(target, ast.Name) and target.id == right.attr for target in stmt.targets)
                for stmt in enum.body
            )
            return member and enum_attribute(left.attr, name)
        if isinstance(right, ast.Constant) and right.value is None:
            hit = Locator(old).innermost(line)
            owner = next((node for node in reversed(hit[3]) if isinstance(node, ast.ClassDef)), None) if hit else None
            return owner is not None and optional_identity_field(path, left.attr, owner)
        return False
    return False


def binding_names(statement):
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {statement.name}
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        return {n.id for target in targets for n in ast.walk(target)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
    if isinstance(statement, (ast.Import, ast.ImportFrom)):
        return {a.asname or a.name.split(".")[0] for a in statement.names}
    return set()


def from_module(consumer, statement):
    if not statement.level:
        return statement.module
    package = consumer.removesuffix(".py").split("/")[:-1]
    components = package[:len(package) - statement.level + 1]
    if statement.module:
        components.append(statement.module)
    return ".".join(components)


def changed_binding_tests(path, old_tree, new_tree, removed, inserted):
    """Capture tests importing public data, and modules importing a deleted binding."""
    edits = set(removed) | set(inserted)
    changed = set()
    possibly_removed = set()
    for statement in old_tree.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if statement.lineno in removed:
                possibly_removed.add(statement.name)
        elif any(statement.lineno <= line <= statement.end_lineno for line in edits):
            names = binding_names(statement)
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                changed.update(names)
            if removed:
                possibly_removed.update(names)

    deleted = set()
    if possibly_removed:
        surviving = set().union(*(binding_names(stmt) for stmt in new_tree.body))
        deleted = possibly_removed - surviving
    if not (changed or deleted):
        return set()

    module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    leaf = module.rsplit(".", 1)[-1]
    affected = set()
    for consumer, text in SRC.items():
        if consumer == path or not consumer.endswith(".py"):
            continue
        names = changed | deleted if consumer.startswith("tests/") else deleted
        if not names or (leaf not in text and "from ." not in text):
            continue
        if not any(name in text for name in names) and "import *" not in text:
            continue
        for stmt in ast.walk(source_tree(consumer)):
            if not isinstance(stmt, ast.ImportFrom) or from_module(consumer, stmt) != module:
                continue
            if not any(alias.name in names or alias.name == "*" for alias in stmt.names):
                continue
            if consumer.startswith("tests/"):
                if consumer not in BY_FILE:
                    return set(ALL)
                affected.update(BY_FILE[consumer])
            else:
                affected.update(importers_all(consumer))
                pool_mask = 0
                for mask in POOL_MAP["lines"].get(consumer, {}).values():
                    pool_mask |= mask
                affected.update(tests_from_pool_mask(pool_mask))
            if affected == ALL:
                return affected
            break
    return affected


@lru_cache(maxsize=1)
def dispatch_map():
    data = load_map("dispatch")
    return set(data["catalog"]) == set(NUM), set(data["sites"]), data["readers"]

@lru_cache(maxsize=64)
def dispatch_constants(path):
    return callback_spans(path, source_tree(path), SRC[path])

def callback_spans(path, tree, source):
    rows = {}
    lines = source.splitlines()
    has_factory = any(isinstance(stmt, ast.ImportFrom) and stmt.module == "sqlglot.dialects.dialect"
                      and any(item.name == "rename_func" and not item.asname for item in stmt.names)
                      for stmt in tree.body)
    for cls in tree.body:
        if not isinstance(cls, ast.ClassDef):
            continue
        for stmt in cls.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1 or not isinstance(stmt.targets[0], ast.Name) or stmt.targets[0].id not in ("FUNCTIONS","TRANSFORMS") or not isinstance(stmt.value, ast.Dict):
                continue
            for callback in stmt.value.values:
                if isinstance(callback, ast.Lambda):
                    constants = [node for node in ast.walk(callback.body) if isinstance(node, ast.Constant) and type(node.value) in (int, bool, str)]
                elif has_factory and isinstance(callback, ast.Call) and isinstance(callback.func, ast.Name) and callback.func.id == "rename_func" and len(callback.args)==1 and not callback.keywords and isinstance(callback.args[0],ast.Constant) and type(callback.args[0].value) is str:
                    constants = callback.args
                else:
                    continue
                site = f"{path}:{callback.lineno}:{callback.col_offset}"
                for value in constants:
                    if value.lineno != value.end_lineno or not lines[value.lineno-1].isascii():
                        continue
                    token = lines[value.lineno-1][value.col_offset:value.end_col_offset]
                    if token != str(value.value) and not (isinstance(value.value,str) and token[:1] in ("'",'"')):
                        continue
                    rows.setdefault(value.lineno,[]).append((value.col_offset,value.end_col_offset,type(value.value).__name__,site))
    return rows

def callback_readers(path, line, before, after):
    if not path.startswith(("sqlglot/parsers/","sqlglot/generators/")):
        return None
    locations = FACTS["dispatch_spans"].get(path,{}) if "dispatch_spans" in FACTS else dispatch_constants(path)
    slots = locations.get(line, ())
    if not slots:
        return None
    catalog,covered,readers = dispatch_map()
    diagnostics = profile()
    if not catalog or path in diagnostics["source_reads"] or line in diagnostics["code_reads"].get(path, ()):
        return None
    for start,end,kind,site in slots:
        prefix,suffix=before[:start],before[end:]
        if site not in covered or not after.startswith(prefix) or not after.endswith(suffix):
            continue
        value=after[start:len(after)-len(suffix)]
        if not value or value==before[start:end] or not value.isascii() or len(value)>256:
            continue
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(value).readline))
            if tokens[0].string != value or tokens[0].type not in (tokenize.STRING,tokenize.NUMBER,tokenize.NAME):
                continue
            parsed=ast.literal_eval(value)
        except (SyntaxError,ValueError,tokenize.TokenError):
            continue
        if type(parsed).__name__ != kind:
            continue
        ids=readers.get(site,())
        if any(node not in NUM for node in ids):
            return None
        return {NUM[node] for node in ids}
    return None

def recursive_comparison_edit(path,old,new,removed,inserted):
    if len(removed)!=1 or len(inserted)!=1:
        return False
    line=removed[0]
    if not any(start<=line<=end and owner.rsplit(".",1)[-1] in {"__eq__","__ne__","__lt__","__le__","__gt__","__ge__"} for start,end,owner in FACTS.get("functions",{}).get(path,())):
        return False
    before=old.splitlines();after=new.splitlines()
    return line<=len(after) and " is " in before[line-1] and any(op in after[line-1] for op in ("==","!=","<",">"))

def analyze_py_source(path, f, reasons, single_file):
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
        return set(ALL)
    new, removed, inserted = apply(old, f["hunks"])
    if recursive_comparison_edit(path,old,new,removed,inserted):
        reasons.append("recursive-comparison")
        return set(ALL)

    if single_file and f["old"]==f["new"] and len(removed)==len(inserted)==1 and inserted[0] in (removed[0],removed[0]+1) and removed[0]>2 and path.startswith(("sqlglot/parsers/","sqlglot/generators/")):
        line=removed[0]
        original,updated=old.split("\n"),new.split("\n")
        if original[:line-1]==updated[:line-1] and original[line:]==updated[line:]:
            readers=callback_readers(path,line,original[line-1].removesuffix("\r"),updated[line-1].removesuffix("\r"))
            if readers is not None:
                reasons.append("module-level:callback")
                return readers
    if single_file and len(removed)==len(inserted)==1 and inserted[0] in (removed[0],removed[0]+1) and "fields" in FACTS and removed[0]>2:
        line = removed[0]
        records = FACTS["fields"].get(path,{}).get(line,())
        if records:
            original,updated = old.split("\n"),new.split("\n")
            if original[:line-1]==updated[:line-1] and original[line:]==updated[line:]:
                reason = indexed_certificate(path,line,original[line-1].removesuffix("\r"),updated[line-1].removesuffix("\r"),records)
                if isinstance(reason,tuple):
                    reason,selected = reason
                    reasons.append(reason)
                    return selected
                if reason:
                    reasons.append(reason)
                    return tests_of(M["opens"].get(path))
    try:
        if changed_encoding(old, new):
            reasons.append("source-encoding")
            return set(ALL)
        if "interior" in FACTS and "\x00" not in new:
            original = old.split("\n")
            edits = [line[1:] for hunk in f["hunks"] for line in hunk["lines"] if line[:1] in ("+","-")]
            if edits and all((not body.strip(" \t\r") or body.lstrip(" \t").startswith("#")) and "\r" not in body.removesuffix("\r") for body in edits) and not set(removed).intersection(FACTS["multiline"].get(path,())) and not set(inserted).intersection(FACTS["interior"].get(path,())) and not any(0 < n <= len(original) and original[n-1].rstrip().endswith("\\") for near in removed+inserted for n in (near-1,near,near+1)):
                reasons.append("comment-only")
                return set()
            if len(removed)==len(inserted)==1 and inserted[0] in (removed[0],removed[0]+1):
                line=removed[0]
                entry=FACTS["statements"].get(path,{}).get(line)
                before,after=original[line-1].removesuffix("\r"),new.split("\n")[line-1].removesuffix("\r")
                if entry and line not in FACTS["multiline"][path] and safe_boolean_edit(before,after):
                    start,end,why=entry
                    reasons.append(why)
                    return cov_range(path,start,end)
        compile(new.encode("utf-8"), path, "exec", dont_inherit=True)
    except (SyntaxError, UnicodeError):
        reasons.append("syntax-error")
        return set(ALL)
    if "statements" in FACTS:
        original = old.splitlines()
        if path in FACTS["multiline"] and all(line[1:].lstrip().startswith("#") or not line[1:].strip() for hunk in f["hunks"] for line in hunk["lines"] if line[:1] in ("+","-")):
            touched = set(removed) | set(inserted)
            if touched and not any(number in FACTS["multiline"][path] or (0 < number <= len(original) and original[number-1].rstrip().endswith("\\")) for near in touched for number in (near-1,near,near+1)):
                reasons.append("comment-only")
                return set()
        if len(removed) == len(inserted) == 1 and inserted[0] in (removed[0],removed[0]+1):
            line = removed[0]
            statement = FACTS["statements"].get(path, {}).get(line)
            before, after = original[line-1], new.splitlines()[line-1]
            if statement and line not in FACTS["multiline"][path] and all(marker not in before for marker in (" is ", "version", "copy=", "lambda")) and before[:len(before)-len(before.lstrip())] == after[:len(after)-len(after.lstrip())] and simple_token_change(before, after):
                start,end,why = statement
                reasons.append(why)
                return cov_range(path,start,end)
    narrow = None
    if len(removed) == len(inserted) == 1 and inserted[0] in (removed[0],removed[0]+1):
        line = removed[0]
        before,after = old.split("\n")[line-1],new.split("\n")[line-1]
        if before[:len(before)-len(before.lstrip())] == after[:len(after)-len(after.lstrip())] and simple_token_change(before,after) and line not in FACTS.get("multiline",{}).get(path,()):
            narrow = line
    old_tree, new_tree = source_tree(path), ast.parse(new)
    if equal_ast(old_tree, new_tree, narrow):
        reasons.append("comment-only")
        return set()
    if single_file and f["old"] == f["new"] and len(removed) == len(inserted) == 1:
        certificate = certified_edit(path, old_tree, new_tree, removed[0])
        if certificate:
            reasons.append(certificate)
            return tests_of(M["opens"].get(path))
    if single_file and f["old"] == f["new"] and equivalent_comparison(path, old, new_tree, removed, inserted):
        reasons.append("equivalent-by-source")
        return tests_of(M["opens"].get(path))
    if single_file and "NUMERIC_ESCAPES" in old and f["old"] == f["new"]:
        readers = value_readers(path, old_tree, new_tree, removed)
        if readers is not None:
            reasons.append("key-value")
            return readers
    changed = added_lines(f["hunks"])
    if eager_failure(new_tree, changed):
        # A broken lazy import can cascade beyond tests that reached the
        # original module, such as a test that iterates available dialects.
        reasons.append("eager-initialization")
        return set(ALL)
    loc = Locator(old, old_tree)
    new_members = added_class_members(loc.tree, new_tree, changed)
    sel = changed_binding_tests(path, old_tree, new_tree, removed, inserted)
    if sel:
        reasons.append("imported-binding")
    if new_members:
        reasons.append("new-class-member")
        sel.update(added_member_readers(path, old, new_members))
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


@lru_cache(maxsize=1)
def test_module_imports():
    """Build reverse imports among the pinned test modules, including package initializers."""
    if "test_importers" in FACTS:
        return FACTS["test_importers"]
    modules = {path[:-3].replace("/", ".").removesuffix(".__init__"): path
               for path in SRC if path.startswith("tests/") and path.endswith(".py")}
    reverse = {}
    for module, path in modules.items():
        package = module if path.endswith("/__init__.py") else module.rpartition(".")[0]
        imported = {package}
        for node in ast.walk(ast.parse(SRC[path])):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    base = ".".join(parts[:len(parts) + 1 - node.level] + ([base] if base else []))
                imported.add(base)
                imported.update(f"{base}.{alias.name}" for alias in node.names)
        for name in imported:
            dependency = modules.get(name)
            if dependency and dependency != path:
                reverse.setdefault(dependency, set()).add(path)
    return reverse


@lru_cache(maxsize=None)
def dependent_test_numbers(path):
    reverse = test_module_imports()
    seen = {path}
    pending = [path]
    result = set()
    while pending:
        for importer in reverse.get(pending.pop(), ()):
            if importer not in seen:
                result.update(BY_FILE.get(importer, ()))
                pending.append(importer)
                seen.add(importer)
    return frozenset(result)


with (HERE / "maps" / "fixture_scopes.json").open() as stream:
    FIXTURE_SCOPES = json.load(stream)


FIXTURE_LINES = {}
with (HERE / "maps" / "fixture_lines.json").open() as stream:
    for scope, files in json.load(stream).items():
        for path, lines in files.items():
            FIXTURE_LINES.setdefault(path, []).append((scope, lines))


@lru_cache(maxsize=None)
def scope_tests(scope):
    return frozenset(number for node_id, number in NUM.items()
                     if not scope or node_id == scope or node_id.startswith((scope + "::", scope + "/")))


def test_file_tests(path, f, reasons):
    """Changes to a tests/*.py file."""
    in_file = set(BY_FILE.get(path, []))
    old = SRC.get(path)
    if old is None or f["new"] is None or f["old"] is None:
        reasons.append("testfile-whole")
        return in_file if path != "tests/helpers.py" else set(ALL)
    new, removed, inserted = apply(old, f["hunks"])
    try:
        if changed_encoding(old, new):
            reasons.append("testfile-encoding")
            return set(ALL)
        compile(new.encode("utf-8"), path, "exec", dont_inherit=True)
    except (SyntaxError, UnicodeError):
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
    for path in SRC:
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
                result.add(f"{path}::{cls.name}::{name}")
    return result


POOL_IDS = FACTS["pool"] if "pool" in FACTS else pool_tests()
UNTRACED_POOL_TESTS = {NUM[node] for node in POOL_IDS if node in NUM}.difference(POOL_NUMBERS)

EXTRA = {NUM[name]: entry for name, entry in load_map("extra-lines").items() if name in NUM}

def extra_tests(path, reasons):
    if reasons == ["key-value"]:
        return set()
    if reasons in (["equivalent-by-source"], ["unequal-tuple-shapes"], ["unused-factory"], ["unchanged-validation"], ["fresh-expression-copy"]):
        return (UNMAPPED - EXTRA.keys()) | {number for number, (raw, _) in EXTRA.items() if path in raw}
    if not reasons or not all(reason in {"stmt", "compound-header", "key-value"} or reason.startswith("module-level:") for reason in reasons):
        return UNMAPPED
    # Keep every isolated test that imported the module or opened its source.
    return (UNMAPPED - EXTRA.keys()) | {number for number, (raw, lines) in EXTRA.items() if path in raw or path in lines}

@lru_cache(maxsize=1)
def key_profile():
    profile = json.load(gzip.open(HERE / "maps/literal-keys.json.gz"))
    return profile["data"], {NUM[test] for test in profile["tests"] if test in NUM}

@lru_cache(maxsize=1)
def profile():
    data = load_map("order-shapes")
    data["factory_sites"] = {json.loads(label)[0] for label in data["factories"]}
    return data

@lru_cache(maxsize=1)
def same_catalog():
    return sorted(NUM) == profile()["catalog"]

def indexed_certificate(path,line,before,after,records):
    if not same_catalog():
        return None
    data = profile()
    for start,end,kind,detail in records:
        prefix, suffix = before[:start], before[end:]
        if not after.startswith(prefix) or not after.endswith(suffix) or len(after)<len(prefix)+len(suffix):
            continue
        old = before[start:end].strip(" \t")
        new = after[start:len(after)-len(suffix)].strip(" \t")
        if kind in ("key","class-int") and len(new)<=32 and re.fullmatch(r"[0-9][0-9a-fA-F_xXoObB]*",new) and old!=new:
            try:
                if type(ast.literal_eval(new)) is not int:
                    continue
            except (ValueError,SyntaxError):
                continue
            if kind=="key":
                slot,key=detail
                readers=key_readers(path,slot,[key])
                if readers is not None:
                    return "key-value",readers
            else:
                owner,field,lambdas=detail
                parent=[ast.ClassDef(name=owner, bases=[],keywords=[],body=[],decorator_list=[],type_params=[])]
                readers=tests_of(M["names"].get(field))
                for lo,hi in lambdas:
                    readers.update(cov_range(path,lo,hi))
                module_users=users(path)
                if module_users:
                    readers &= module_users
                readers.update(observed_attribute_tests(path,parent,{field}))
                readers.update(pool_attribute_readers(path,parent,{field}))
                return "module-level:"+field,readers
        if kind in ("enum","identity") and (old,new) in (("is","=="),("is not","!=")) and (kind=="identity" or FACTS["enum"].get(detail)):
            return "equivalent-by-source"
        if path in data["source_reads"] or line in data["code_reads"].get(path,()):
            continue
        if kind=="order" and {old,new} in ({"<","<="},{">",">="}) and FACTS["version"] and data["sites"].get(detail)==[["unequal","length"]]:
            return "unequal-tuple-shapes"
        if {old,new} != {"True","False"}:
            continue
        if kind=="owned" and data["arguments"].get(detail):
            return "fresh-expression-copy"
        if kind=="required":
            owner,key=detail
            if owner in data["missing"] and owner not in data["opaque"] and key not in data["missing"][owner]:
                return "unchanged-validation"
        if kind=="factory":
            site,arguments=detail
            if site in data["factory_sites"] and json.dumps([site,None,None]) not in data["factories"] and arguments not in data["factories"]:
                return "unused-factory"

def only_field(first, second, old, new, field):
    previous = getattr(new, field)
    setattr(new, field, getattr(old, field))
    try:
        return equal_ast(first, second)
    finally:
        setattr(new, field, previous)

@lru_cache(maxsize=1)
def fixed_dialect_versions():
    if "version" in FACTS:
        return FACTS["version"]
    definition = "sqlglot/dialects/dialect.py"
    writes = []
    for file in WRITE_PATHS.get("version", ()):
        for node in ast.walk(source_tree(file)):
            if isinstance(node, ast.Attribute) and node.attr == "version" and isinstance(node.ctx, (ast.Del, ast.Store)):
                writes.append(file)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {"setattr", "delattr"} and any(isinstance(arg, ast.Constant) and arg.value == "version" for arg in node.args):
                return False
    pattern = 'parts = str(kwargs.pop("version", sys.maxsize)).split(".")\nparts.extend(["0"] * (3 - len(parts)))\nself.version = tuple(int(p) for p in parts[:3])'
    required = ast.parse(pattern).body
    return writes == [definition] and any(isinstance(cls, ast.ClassDef) and cls.name == "Dialect" and any(isinstance(fn, ast.FunctionDef) and fn.name == "__init__" and equal_ast(fn.body[:3], required) for fn in cls.body) for cls in source_tree(definition).body)

def factory_arguments(call):
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        return None
    if any(not isinstance(value, ast.Constant) or type(value.value) not in (str, bool, int, type(None)) for value in [*call.args, *(kw.value for kw in call.keywords)]) or any(kw.arg is None for kw in call.keywords):
        return None
    return [item.value for item in call.args], {kw.arg: kw.value.value for kw in call.keywords}

@lru_cache(maxsize=None)
def pure_factory(path, name):
    tree = source_tree(path)
    definition = path
    for item in tree.body:
        if isinstance(item, ast.ImportFrom) and any((alias.asname or alias.name) == name for alias in item.names):
            mod = from_module(path, item)
            definition = module_path(mod) if mod else None
            if not definition:
                return None
            break
    for fn in source_tree(definition).body:
        if not isinstance(fn, ast.FunctionDef) or fn.name != name or fn.decorator_list or not fn.args.vararg or not fn.args.kwarg or fn.args.args or fn.args.kwonlyargs or len(fn.body) != 2:
            continue
        inner, ret = fn.body
        if not isinstance(inner, ast.FunctionDef) or inner.decorator_list or len(inner.body) != 1 or not isinstance(ret, ast.Return) or not isinstance(ret.value, ast.Name) or ret.value.id != inner.name:
            continue
        if inner.args.defaults or inner.args.kw_defaults or not isinstance(inner.body[0], ast.Return):
            continue
        return definition + ":" + name

def certified_edit(path, first, second, line):
    if not same_catalog():
        return None
    data = profile()
    if path in data["source_reads"] or line in data["code_reads"].get(path, ()):
        return None
    original_line = SRC[path].splitlines()[line - 1]
    if "copy=" in original_line and "from sqlglot import expressions as exp" in SRC[path]:
        for old,new in zip(ast.walk(first),ast.walk(second)):
            if not isinstance(old,ast.Call) or not isinstance(new,ast.Call) or old.lineno != line or len(old.args) != 1 or not isinstance(old.args[0],ast.Call):
                continue
            if ast.unparse(old.func) != "exp.alias_" or ast.unparse(old.args[0].func) != "exp.table_" or len(old.args[0].args) != 1 or old.args[0].keywords:
                continue
            if {kw.arg for kw in old.keywords} != {"alias","copy"} or len(old.keywords) != 2 or len(new.keywords) != 2:
                continue
            site = f"{path}:{line}:{old.col_offset}"
            if not data.get("arguments",{}).get(site):
                continue
            before = next(kw.value for kw in old.keywords if kw.arg == "copy")
            after = next((kw.value for kw in new.keywords if kw.arg == "copy"), None)
            if isinstance(before,ast.Constant) and isinstance(after,ast.Constant) and type(before.value) is bool and type(after.value) is bool and before.value != after.value and only_field(first,second,before,after,"value"):
                return "fresh-expression-copy"
    for old, new in zip(ast.walk(first), ast.walk(second)) if "version" in SRC[path].splitlines()[line - 1] else ():
        if getattr(old, "lineno", None) != line or type(old) is not type(new):
            continue
        if isinstance(old, ast.Compare) and len(old.ops) == len(new.ops) == 1 and isinstance(old.left, ast.Attribute) and old.left.attr == "version" and self_field(old.left.value, "dialect") and isinstance(old.comparators[0], ast.Tuple) and len(old.comparators[0].elts) != 3:
            pair = {type(old.ops[0]), type(new.ops[0])}
            if pair not in ({ast.Lt, ast.LtE}, {ast.Gt, ast.GtE}):
                continue
            site = f"{path}:{line}:{old.col_offset}"
            if data["sites"].get(site) == [["unequal","length"]] and fixed_dialect_versions() and only_field(first, second, old, new, "ops"):
                return "unequal-tuple-shapes"
    if not path.startswith(("sqlglot/expressions/", "sqlglot/parsers/")):
        return None
    for cls, target in zip(first.body, second.body):
        if not isinstance(cls, ast.ClassDef) or not isinstance(target, ast.ClassDef) or not (cls.lineno <= line <= cls.end_lineno):
            continue
        for stmt, peer in zip(cls.body, target.body):
            if not (isinstance(stmt, ast.Assign) and isinstance(peer, ast.Assign) and stmt.lineno <= line <= stmt.end_lineno and isinstance(stmt.value, ast.Dict) and isinstance(peer.value, ast.Dict) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
                continue
            field = stmt.targets[0].id
            for key, old, new in zip(stmt.value.keys, stmt.value.values, peer.value.values):
                if not isinstance(key, ast.Constant) or type(key.value) is not str or old.lineno != line or type(old) is not type(new):
                    continue
                if field == "arg_types" and isinstance(old, ast.Constant) and type(old.value) is bool and type(new.value) is bool and old.value != new.value:
                    owner = path[:-3].replace("/", ".") + ":" + cls.name
                    if not path.startswith("sqlglot/expressions/") or cls.decorator_list or cls.name in BASE_NAMES or len(cls.bases) != 1 or not isinstance(cls.bases[0], ast.Name) or cls.bases[0].id != "Expression" or len(cls.body) != 1:
                        continue
                    if owner not in data["missing"] or owner in data["opaque"] or key.value in data["missing"][owner]:
                        continue
                    if only_field(first, second, old, new, "value"):
                        return "unchanged-validation"
                if field == "SHOW_PARSERS" and path.startswith("sqlglot/parsers/") and isinstance(old, ast.Call):
                    args = factory_arguments(old)
                    if not args or not factory_arguments(new) or not equal_ast(old.func, new.func):
                        continue
                    site = pure_factory(path, old.func.id)
                    if site is None or site not in data["factory_sites"] or json.dumps([site,None,None]) in data["factories"] or json.dumps([site,*args], sort_keys=True) in data["factories"]:
                        continue
                    if equal_ast(old.args, new.args) and [(kw.arg,type(kw.value.value)) for kw in old.keywords] == [(kw.arg,type(kw.value.value)) for kw in new.keywords] and only_field(first, second, old, new, "keywords"):
                        return "unused-factory"
    return None

def value_readers(path, old_tree, new_tree, removed):
    if len(removed) != 1:
        return None
    line = removed[0]
    def locate(tree):
        for cls in tree.body:
            if not isinstance(cls, ast.ClassDef):
                continue
            for owner in cls.body:
                if not isinstance(owner, ast.ClassDef):
                    continue
                for stmt in owner.body:
                    if isinstance(stmt, ast.Assign) and stmt.lineno <= line <= stmt.end_lineno and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name) and stmt.targets[0].id == "NUMERIC_ESCAPES" and isinstance(stmt.value, ast.Dict):
                        return cls.name + "." + owner.name, stmt
    first, second = locate(old_tree), locate(new_tree)
    if not first or not second or first[0] != second[0]:
        return None
    original, changed = first[1].value, second[1].value
    if not equal_ast(original.keys, changed.keys) or not all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in original.keys):
        return None
    keys = [key.value for key, before, after in zip(original.keys, original.values, changed.values) if not equal_ast(before, after)]
    if not keys or not all(isinstance(value, ast.Tuple) and len(value.elts) == 4 and all(isinstance(item, ast.Constant) and type(item.value) is int for item in value.elts) for value in original.values + changed.values):
        return None
    second[1].value = original
    try:
        if not equal_ast(old_tree, new_tree):
            return None
    finally:
        second[1].value = changed
    slot = path[:-3].replace("/", ".") + ":" + first[0] + ":NUMERIC_ESCAPES"
    return key_readers(path,slot,keys)

def key_readers(path,slot,keys):
    profile, known = key_profile()
    accesses = profile.get(slot)
    if accesses is None or "" in accesses:
        return None
    extra = isolated_calls()
    selected = {number for number in UNMAPPED if number not in extra or path in extra[number][0] or path in EXTRA.get(number,[[],{}])[0] or set(extra[number][2].get(slot, ())).intersection(["*", *keys])}
    return selected | (ALL - known - UNMAPPED) | {NUM[test.partition("[")[0]] for test, reads in accesses.items() if test.partition("[")[0] in NUM and (None in reads or set(keys).intersection(reads))}

@lru_cache(maxsize=1)
def isolated_calls():
    return {NUM[node]: info for node, info in load_map("extra-calls").items() if node in NUM}

def local_function_readers(path, patch, selected):
    updated, removed, inserted = apply(SRC[path], patch["hunks"])
    if len(removed) != 1 or len(inserted) != 1:
        return selected
    line = removed[0]
    old_lines, new_lines = SRC[path].splitlines(), updated.splitlines()
    if len(old_lines) != len(new_lines) or not simple_token_change(old_lines[line-1], new_lines[line-1]):
        return selected
    owner = containing_function(path, line)
    if owner is None:
        return selected
    profile_data = profile()
    if path in profile_data["source_reads"] or line in profile_data["code_reads"].get(path, ()):
        return selected
    details = isolated_calls()
    return {number for number in selected if number not in details or path in details[number][0] or path in EXTRA[number][0] or owner in details[number][1].get(path, ()) or owner in details[number][3].get(path, ())}

def simple_token_change(left, right):
    try:
        a, b = [[(t.type,t.string) for t in tokenize.generate_tokens(io.StringIO(text.strip()).readline)] for text in (left,right)]
    except (tokenize.TokenError, IndentationError):
        return False
    if len(a) != len(b):
        longer, shorter = (a,b) if len(a)>len(b) else (b,a)
        return len(longer)==len(shorter)+1 and any(token == (tokenize.NAME,"not") and longer[:index]+longer[index+1:] == shorter for index,token in enumerate(longer))
    diff = [(x,y) for x,y in zip(a,b) if x != y]
    if len(diff) != 1:
        return False
    (x,u),(y,v) = diff[0]
    return x == y and (x in (tokenize.NUMBER, tokenize.STRING) or {u,v} <= {"True","False"} or {u,v} <= {"<",">","<=",">=","==","!="} or {u,v} <= {"and","or"})

def safe_boolean_edit(before,after):
    for match in re.finditer(r"\b(?:True|False|and|or|not)\b|<=|>=|!=|==|<|>",before):
        replacements = {"True":("False",),"False":("True",),"and":("or",),"or":("and",),"not":("",),"==":("!=",),"!=":("==",),"<":("<=",">"),">":(">=","<"),"<=":("<",">="),">=":(">","<=")}
        for value in replacements[match[0]]:
            end=match.end() + int(not value and before[match.end():].startswith(" "))
            if after==before[:match.start()]+value+before[end:] and simple_token_change(before,after):
                return True
    return False

@lru_cache(maxsize=256)
def containing_function(path, line):
    if "functions" in FACTS:
        return next((name for start, end, name in FACTS["functions"].get(path, ()) if start <= line <= end), None)
    return next((name for start, end, name in function_spans(source_tree(path)) if start <= line <= end), None)

def function_spans(tree):
    result = []
    def walk(body, prefix):
        for node in body:
            if isinstance(node, ast.ClassDef):
                walk(node.body, prefix + node.name + ".")
            elif isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                if node.body and len(node.body) > 0:
                    result.append((node.body[0].lineno, node.end_lineno, prefix + node.name))
                walk(node.body, prefix + node.name + ".<locals>.")
    walk(tree.body, "")
    return sorted(result, key=lambda span: span[1]-span[0])


def analyze(text):
    reasons = []
    sel = set()
    files = parse_patch(text)
    for f in files:
        path = f["new"] or f["old"]
        if path.endswith(".py"):
            sel.update(tests_of(M["opens"].get(path)))
        if path.startswith("sqlglot/") and path.endswith(".py"):
            file_reasons = []
            got = analyze_py_source(path, f, file_reasons, len(files) == 1)
            reasons.extend(file_reasons)
            if file_reasons != ["comment-only"]:
                extra = extra_tests(path, file_reasons)
                if len(files) == 1 and extra and all(reason in {"stmt", "compound-header"} for reason in file_reasons):
                    extra = local_function_readers(path, f, extra)
                got |= UNTRACED_POOL_TESTS | extra
            sel |= got
            if path.startswith("sqlglot/typing/") and "comment-only" not in file_reasons:
                sel |= set(BY_FILE.get("tests/test_typing.py", []))
        elif path.rsplit("/", 1)[-1] == "conftest.py" or path in {
            "pytest.ini", ".pytest.ini", "pyproject.toml", "tox.ini", "setup.cfg",
        }:
            # Pytest loads configuration and hooks without an explicit import
            # from a catalogued test, and hooks can affect the whole session.
            reasons.append("pytest-configuration")
            sel.update(ALL)
        elif path.startswith("tests/") and path.endswith(".py"):
            file_reasons = []
            sel |= test_file_tests(path, f, file_reasons)
            reasons.extend(file_reasons)
            if file_reasons != ["testfile-comment"]:
                sel.update(dependent_test_numbers(path))
        elif path.startswith("tests/"):
            for scope in FIXTURE_SCOPES.get(path, ()):
                sel.update(scope_tests(scope))
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


# A worker can carry more tests while it waits for the busiest worker.
ZERO_COST_TESTS = {n for n in ALL if DUR[n] == 0}
TEST_FILE = {t["number"]: t["file"] for t in TJ}


def worker_cost(workers):
    peak = max((sum(DUR[n] for n in worker) for worker in workers), default=0)
    return peak + WORKER_MS * len(workers)


@lru_cache(maxsize=1)
def full_schedule():
    workers = split(ALL)
    return worker_cost(workers), workers


def covered_schedule(text, selected):
    """Prefer full coverage at equal cost, then fill idle capacity in the chosen schedule."""
    workers = split(selected)
    if not workers or len(selected) == len(ALL):
        return workers
    full_cost, all_workers = full_schedule()
    if full_cost <= worker_cost(workers):
        return all_workers
    loads = [sum(DUR[n] for n in w) for w in workers]
    peak = max(loads)
    remaining = [peak - load for load in loads]
    workers[0].extend(sorted(ZERO_COST_TESTS - selected))
    if any(remaining):
        file_users = set()
        related_files = {TEST_FILE[t] for t in selected - UNMAPPED}
        symbol_readers = set()
        for patch in parse_patch(text):
            path = patch["old"] or patch["new"]
            if not path:
                continue
            if path.startswith("sqlglot/"):
                file_users.update(users(path))
            for hunk in patch["hunks"]:
                for line in hunk["lines"]:
                    if line.startswith(("+", "-")):
                        for name in re.findall(r"[A-Za-z_]\w*", line[1:]):
                            if len(name) > 2:
                                symbol_readers.update(tests_of(M["names"].get(name)))

        def rank(n):
            file_hit = n in file_users
            symbol_hit = n in symbol_readers
            return (not (file_hit and symbol_hit), not file_hit, not symbol_hit,
                    TEST_FILE[n] not in related_files, DUR[n], n)

        largest_gap = max(remaining)
        possible = [n for n in ALL - selected if 0 < DUR[n] <= largest_gap]
        for n in sorted(possible, key=rank):
            size = DUR[n]
            if size > largest_gap:
                continue
            worker = min((i for i, gap in enumerate(remaining) if gap >= size),
                         key=remaining.__getitem__)
            workers[worker].append(n)
            remaining[worker] -= size
            largest_gap = max(remaining)
            if not largest_gap:
                break
    return workers


def main():
    total = 0
    for p in sorted((DATA / "commits").glob("*.patch")):
        text = ""
        try:
            text = p.read_text("utf-8", errors="replace")
            sel, reasons = analyze(text)
            selector=sys.modules[__name__]
            combined=compound.select(selector,text,sel)
            if "recursive-comparison" in reasons:
                sel=set(ALL)
            elif combined is not None:
                sel=combined
            else:
                preserve=replay_edit.preserve(selector,text,sel)
                sel=identity_select.refine(selector,text,sel)
                sel=flag_readers.refine(selector,text,sel)
                sel=predicate_select.refine(selector,text,sel)
                sel=condition_select.refine(selector,text,sel)
                precise=query_phases.refine(selector,text,sel)
                sel=query_replay.refine(selector,text,sel if precise is None else precise) | preserve
            sel=default_inputs.refine(selector,text,sel,list(dict.fromkeys(reasons)))
            sel=validation_inputs.refine(selector,text,sel)
            sel=fixture_inputs.refine(selector,text,sel)
            if "recursive-comparison" not in reasons:
                sel=instruction_impact.refine(selector,text,sel)
            sel=observed_inputs.refine(selector,text,sel)
            sel=isolated_imports.refine(selector,text,sel)
            if "recursive-comparison" not in reasons:
                sel=compound.fixture_mix(selector,text,sel)
            sel=parser_table_keys.refine(selector,text,sel)
            sel=schema_keys.refine(selector,text,sel)
            sel=registry_inputs.include(selector,text,sel)
            sel=lexical_inputs.refine(selector,text,sel)
            sel=get_inputs.refine(selector,text,sel)
            if reasons == ["def-header"]:
                sel=constructor_inputs.refine(selector,text,sel)
        except Exception as e:  # never crash: run everything
            sel, reasons = set(ALL), ["error:" + repr(e)[:80]]
        workers = covered_schedule(text, sel)
        cost = worker_cost(workers)
        total += cost
        log(f"{p.stem}: {sum(map(len, workers))} tests, cost {cost/1000:.1f}s, {sorted(set(reasons))[:6]}")
        print(json.dumps({"commit": p.stem, "workers": workers}, separators=(",", ":")))
    log(f"estimated simulated total {total/1000:.1f}s")


if __name__ == "__main__":
    main()
