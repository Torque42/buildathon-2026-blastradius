"""Check the exact method bindings and initialized data used by the parser stress workload."""

import ast
from collections import defaultdict
from functools import lru_cache
import gzip
import hashlib
import marshal
import re
import sys

import binding_state
import instruction_impact
import workload_replay as workload

DYNAMIC = {"__getattr__", "__getattribute__", "__setattr__", "__delattr__", "__init_subclass__",
           "__new__", "__bases__", "__class__", "__subclasses__", "__dict__", "__builtins__",
           "globals", "locals", "exec", "eval", "setattr", "delattr", "sys", "os", "open",
           "import_module", "settrace", "setprofile", "getframe", "_getframe"}


def pure(node):
    if node is None:
        return True
    if isinstance(node, (ast.Constant, ast.Name)):
        return True
    if isinstance(node, ast.Attribute):
        return not node.attr.startswith("__") and pure(node.value)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(pure(x) for x in node.elts)
    if isinstance(node, ast.Dict):
        return all(pure(x) for x in node.keys + node.values)
    if isinstance(node, ast.Lambda):
        return all(pure(x) for x in node.args.defaults + node.args.kw_defaults)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub, ast.Not)):
        return isinstance(node.operand, ast.Constant)
    return False


def same(a, b):
    return a is b or a is not None and b is not None and ast.dump(a) == ast.dump(b)


def data_delta(a, b):
    if same(a, b) or pure(a) and pure(b):
        return True
    if not isinstance(a, ast.Dict) or not isinstance(b, ast.Dict):
        return False
    # Unchanged side-effectful entries must appear in exactly the same order.
    def effects(node):
        return [(ast.dump(k) if k else None, ast.dump(v)) for k,v in zip(node.keys,node.values)
                if not pure(k) or not pure(v)]
    return effects(a) == effects(b)


def describe(body):
    fields, order = {}, []
    for index, st in enumerate(body):
        if isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str):
            key = ("doc", str(index))
        elif isinstance(st, ast.ClassDef):
            key = ("class", st.name)
        elif isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
            key = ("function", st.name)
        elif isinstance(st, ast.Assign) and len(st.targets)==1 and isinstance(st.targets[0],ast.Name):
            key = ("data", st.targets[0].id)
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target,ast.Name):
            key = ("data", st.target.id)
        else:
            key = ("statement", ast.dump(st))
        occurrence = 0
        while (*key,occurrence) in fields:
            occurrence += 1
        key = (*key,occurrence)
        order.append(key)
        fields[key] = st
    return fields, order


def simple_header(fn):
    return (all(isinstance(d, ast.Name) and d.id in {"classmethod","staticmethod","property"} for d in fn.decorator_list)
            and all(pure(v) for v in fn.args.defaults + fn.args.kw_defaults))


def namespace_changes(a, b):
    old, first = describe(a)
    new, last = describe(b)
    common = old.keys() & new.keys()
    if [k for k in first if k in common] != [k for k in last if k in common]:
        return False
    for key in old.keys() | new.keys():
        left, right = old.get(key), new.get(key)
        if same(left, right):
            continue
        kind, name, _ = key
        if kind == "doc":
            continue
        if kind == "class":
            if left is None or right is None:
                return False
            fields = ("bases", "keywords", "decorator_list", "type_params")
            if any([ast.dump(n) for n in getattr(left, f, ())] !=
                   [ast.dump(n) for n in getattr(right, f, ())] for f in fields):
                return False
            if not namespace_changes(left.body,right.body):
                return False
        elif kind == "function":
            if name in DYNAMIC:
                return False
            if left is None or right is None:
                if not simple_header(left or right):
                    return False
            else:
                if [ast.dump(d) for d in left.decorator_list] != [ast.dump(d) for d in right.decorator_list]:
                    return False
                if not same(left.args, right.args) and not (simple_header(left) and simple_header(right)):
                    return False
        elif kind == "data":
            if name in DYNAMIC or not data_delta(getattr(left,"value",None),getattr(right,"value",None)):
                return False
        else:
            return False
    return True


@lru_cache(22)
def syntax(source):
    return ast.parse(source)


@lru_cache(18)
def compiled(path, source):
    index = defaultdict(list)
    for _, code in instruction_impact.lineage(compile(source, path, "exec", dont_inherit=True)):
        index[code.co_qualname].append(code)
    return index


def prefix_unchanged(selector, edits, text):
    # The patched package is imported for real. Limit namespace changes to
    # ordinary deterministic declarations and edits within function bodies.
    lines = [s[1:] for p in selector.parse_patch(text) for h in p["hunks"] for s in h["lines"]
             if s.startswith(("+","-"))]
    if DYNAMIC & set(re.findall(r"\w+", "\n".join(lines))):
        return False
    version, _, labels, indexed = workload.node_profile()
    if tuple(version) != sys.version_info[:2]:
        return False
    mask = (1 << labels.index("2")) | (1 << labels.index("3"))
    for path, after in edits.items():
        before = selector.SRC[path]
        if not namespace_changes(syntax(before).body, syntax(after).body):
            return False
        old, new = compiled(path, before), compiled(path, after)
        for (file, name, line), variants in indexed.items():
            if file != path:
                continue
            for digest, groups in variants.items():
                hits = [offsets for offsets, stages in groups.items() if stages & mask]
                if not hits:
                    continue
                a = [c for c in old[name] if c.co_firstlineno==line and
                     hashlib.blake2s(c.co_code, digest_size=12).digest()==digest]
                choices = new.get(name, ())
                if len(a)!=1 or len(choices)!=1:
                    return False
                b = choices[0]
                if binding_state.code_hash(a[0]) == binding_state.code_hash(b):
                    continue
                changed = instruction_impact.offset_mask(a[0], b)
                if changed is None or any(v & changed for v in hits):
                    return False
    return True


@lru_cache(1)
def reference():
    return binding_state.profile()


@lru_cache(1)
def inspected_files():
    version, opened, inspected = marshal.loads(gzip.decompress((binding_state.ROOT/"maps/node-inspection.gz").read_bytes()))
    return version, set(opened) | set(inspected)


def refine(selector, text, selected):
    node = selector.NUM.get(workload.NODE)
    if node not in selected or "sqlglot" in sys.modules:
        return selected
    gain = selector.worker_cost(selector.split(selected)) - selector.worker_cost(selector.split(selected-{node}))
    if gain < 2800:
        return selected
    try:
        prepared = workload.prepare(selector,text)
        if not prepared:
            return selected
        edits, _, _, fixtures = prepared
        audit_version, observed_files = inspected_files()
        if (tuple(audit_version)!=sys.version_info[:2] or fixtures or observed_files.intersection(edits)
                or not prefix_unchanged(selector, edits, text)):
            return selected
        version, baseline = reference()
        if tuple(version)!=sys.version_info[:2] or any(row["unknown"] for row in baseline.values()):
            return selected
        # A patch that introduces a new dynamically resolved super member
        # could alter dispatch even when the old implementation was never entered.
        words = {t for p in selector.parse_patch(text) for h in p["hunks"] for line in h["lines"]
                 if line.startswith(("+","-")) for t in re.findall(r"\w+",line[1:])}
        if any(words.intersection(row["dynamic"]) for row in baseline.values()):
            return selected
        _, calls, _, _ = workload.node_profile()
        expected = {key:error for key,length,_,error in calls if length>=100_000 and error}
        def verify(key):
            phase = str(next(i for i,row in enumerate(calls) if row[0]==key))
            truth = baseline[phase]["values"]
            values, unknown = binding_state.live(truth)
            return not unknown and values == truth
        if len(expected)==2 and workload.run_node(edits, expected, check=verify):
            print("runtime bindings verified parser stress test", file=sys.stderr)
            return selected-{node}
    except (KeyError, OSError, TypeError, SyntaxError, ValueError, RecursionError, AttributeError):
        pass
    return selected
