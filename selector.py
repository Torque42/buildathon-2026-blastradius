"""Blast radius selector: map each patch to tests with a per-test coverage map
built at the pinned sqlglot commit, then split them over workers.

Reads /data (or argv[1]), prints one JSON line per commit, logs to stderr."""

import ast
import builtins
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


def equal_ast(left, right):
    """Compare Python syntax without materializing strings for both trees."""
    pending = [(left, right)]
    while pending:
        a, b = pending.pop()
        if a is b:
            continue
        if type(a) is not type(b):
            return False
        if isinstance(a, ast.AST):
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


def enum_attribute(field, enum_name):
    """Require every syntactically visible write to produce an enum member."""
    pending = [field]
    examined = set()
    while pending:
        name = pending.pop()
        if name in examined:
            continue
        examined.add(name)
        writes = 0
        for path, text in SRC.items():
            if not path.endswith(".py") or name not in text:
                continue
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
    for source, blob in SRC.items():
        if not source.endswith(".py") or class_type not in blob:
            continue
        for declaration in ast.walk(source_tree(source)):
            if isinstance(declaration, ast.ClassDef) and any(
                class_type in names_in(base) for base in declaration.bases
            ):
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
        return importers_all(path)
    new, removed, inserted = apply(old, f["hunks"])
    try:
        compile(new, path, "exec", dont_inherit=True)
    except SyntaxError:
        reasons.append("syntax-error")
        return importers_all(path)
    old_tree, new_tree = source_tree(path), ast.parse(new)
    if equal_ast(old_tree, new_tree):
        if new.split("\n")[:2] != old.split("\n")[:2]:
            reasons.append("header-lines")  # coding cookie territory
            return importers_all(path)
        reasons.append("comment-only")
        return set()
    if single_file and f["old"] == f["new"] and equivalent_comparison(path, old, new_tree, removed, inserted):
        reasons.append("equivalent-by-source")
        return tests_of(M["opens"].get(path))
    changed = added_lines(f["hunks"])
    if eager_failure(new_tree, changed):
        # A broken lazy import can cascade beyond tests that reached the
        # original module, such as a test that iterates available dialects.
        reasons.append("eager-initialization")
        return set(ALL)
    loc = Locator(old, old_tree)
    new_members = added_class_members(loc.tree, new_tree, changed)
    sel = set()
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


@lru_cache(maxsize=None)
def importing_test_files(provider):
    """Find test modules that depend on a helper, including transitive imports."""
    pending = [provider]
    dependents = set()
    while pending:
        path = pending.pop()
        module = path.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        for source, text in SRC.items():
            if not source.startswith("tests/") or not source.endswith(".py"):
                continue
            if source == provider or source in dependents:
                continue
            if module.split(".")[-1] not in text:
                continue
            package = source.rsplit("/", 1)[0].replace("/", ".").split(".")
            for node in ast.walk(source_tree(source)):
                imported = set()
                if isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom):
                    prefix = package[:len(package) + 1 - node.level] if node.level else []
                    scope = ".".join(prefix + ([node.module] if node.module else []))
                    imported.add(scope)
                    imported.update(scope + "." + alias.name for alias in node.names)
                if module in imported:
                    dependents.add(source)
                    pending.append(source)
                    break
    return frozenset(dependents)


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
    files = parse_patch(text)
    for f in files:
        path = f["new"] or f["old"]
        if path.startswith("sqlglot/") and path.endswith(".py"):
            file_reasons = []
            got = analyze_py_source(path, f, file_reasons, len(files) == 1)
            reasons.extend(file_reasons)
            if file_reasons != ["comment-only"]:
                got |= UNTRACED_POOL_TESTS | UNMAPPED
            sel |= got
            if path.startswith("sqlglot/typing/") and "comment-only" not in file_reasons:
                sel |= set(BY_FILE.get("tests/test_typing.py", []))
        elif path.startswith("tests/") and path.endswith(".py"):
            file_reasons = []
            sel |= test_file_tests(path, f, file_reasons)
            reasons.extend(file_reasons)
            if file_reasons != ["testfile-comment"]:
                for importer in importing_test_files(path):
                    sel.update(BY_FILE.get(importer, ()))
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


# A worker can carry more tests while it waits for the busiest worker.
ZERO_COST_TESTS = {n for n in ALL if DUR[n] == 0}
TEST_FILE = {t["number"]: t["file"] for t in TJ}


def covered_schedule(text, selected):
    """Fill idle worker capacity while preserving the selected tests and their assignments."""
    workers = split(selected)
    if not workers or len(selected) == len(ALL):
        return workers
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
        except Exception as e:  # never crash: run everything
            sel, reasons = set(ALL), ["error:" + repr(e)[:80]]
        workers = covered_schedule(text, sel)
        cost = (max(sum(DUR[n] for n in w) for w in workers) + WORKER_MS * len(workers)) if workers else 0
        total += cost
        log(f"{p.stem}: {sum(map(len, workers))} tests, cost {cost/1000:.1f}s, {sorted(set(reasons))[:6]}")
        print(json.dumps({"commit": p.stem, "workers": workers}, separators=(",", ":")))
    log(f"estimated simulated total {total/1000:.1f}s")


if __name__ == "__main__":
    main()
