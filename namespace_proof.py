"""Accept deterministic namespace declarations and changes inside existing Python functions."""
import ast
from functools import lru_cache

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
        kind, name, _ = key
        # Matching class and ordinary method bodies are checked below. Dumping
        # their entire syntax trees first duplicates work on large parser files.
        check_header = left is not None and right is not None and (
            kind == 'class' or kind == 'function' and name not in DYNAMIC)
        if not check_header and same(left, right):
            continue
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



@lru_cache(20)
def declarations_only(before,after):
    return namespace_changes(ast.parse(before).body,ast.parse(after).body)
