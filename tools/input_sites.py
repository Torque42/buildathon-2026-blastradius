"""Find calls whose scalar input is used only inside the returned closure."""
import ast
from pathlib import Path

SCALARS = (str, int, bool)

def scalar_key(value):
    return f"{type(value).__name__}:{value!r}"

def module_key(path):
    return path[:-3].replace("/", ".").removesuffix(".__init__")

def scan(source_root):
    sources = {str(path.relative_to(source_root)): ast.parse(path.read_bytes())
               for path in sorted((source_root / "sqlglot").rglob("*.py"))}
    factories = {}
    definitions = {}
    guards = {}
    for path, tree in sources.items():
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.BoolOp):
                present = any(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                              and n.value.id == "sys" and n.attr == "modules" for n in ast.walk(node.value))
                if present:
                    guards.setdefault(path, []).append([node.value.lineno, node.value.col_offset,
                                                         node.value.end_col_offset])
            if not isinstance(node, ast.FunctionDef) or node.decorator_list:
                continue
            body = list(node.body)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                body = body[1:]
            if not body or not isinstance(body[-1], ast.Return):
                continue
            inner = body[-1].value
            if len(body) == 2 and isinstance(body[0], ast.FunctionDef) and isinstance(inner, ast.Name) and inner.id == body[0].name:
                inner = body[0]
                if inner.decorator_list or inner.args.defaults or any(inner.args.kw_defaults):
                    continue
            elif len(body) != 1 or not isinstance(inner, ast.Lambda):
                continue
            contents = list(inner.body) if isinstance(inner.body, list) else [inner.body]
            elements = [n for stmt in contents for n in ast.walk(stmt)]
            if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef, ast.Global, ast.Nonlocal))
                   or isinstance(n, ast.Name) and n.id in {"eval", "exec", "locals", "vars", "dir", "globals"} for n in elements):
                continue
            params = [a.arg for a in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs)]
            loads = {n.id for n in elements if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
            writes = {n.id for n in elements if isinstance(n, ast.Name) and not isinstance(n.ctx, ast.Load)}
            inner_args = {a.arg for a in ast.walk(inner.args) if isinstance(a, ast.arg)}
            captured = sorted(set(params) & loads - writes - inner_args)
            # Defaults and annotations in the nested function run when making the closure.
            evaluated = [inner.args, *getattr(inner, "decorator_list", [])]
            if getattr(inner, "returns", None):
                evaluated.append(inner.returns)
            captured = [p for p in captured if not any(isinstance(n, ast.Name) and n.id == p for v in evaluated for n in ast.walk(v))]
            if captured:
                key = f"{module_key(path)}.{node.name}"
                factories[key] = (params, set(captured))
                definitions.setdefault(path, {})[node.name] = captured
    sites = {}
    for path, tree in sources.items():
        aliases = {}
        for st in tree.body:
            if isinstance(st, ast.ImportFrom) and st.module and not st.level:
                for n in st.names:
                    aliases[n.asname or n.name] = f"{st.module}.{n.name}"
            if isinstance(st, ast.FunctionDef):
                aliases[st.name] = f"{module_key(path)}.{st.name}"
        for table in (n for n in ast.walk(tree) if isinstance(n, ast.Dict)):
            for key, call in zip(table.keys, table.values):
                if not (isinstance(key, ast.Constant) and type(key.value) is str and isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name) and aliases.get(call.func.id) in factories):
                    continue
                factory = aliases[call.func.id]
                params, captured = factories[factory]
                if any(isinstance(a, ast.Starred) for a in call.args) or any(kw.arg is None for kw in call.keywords):
                    continue
                args = list(zip(params, call.args)) + [(kw.arg, kw.value) for kw in call.keywords]
                for param, literal in args:
                    if param not in captured or not isinstance(literal, ast.Constant) or type(literal.value) not in SCALARS:
                        continue
                    if literal.lineno == literal.end_lineno:
                        sites.setdefault(path, []).append([literal.lineno, literal.col_offset, literal.end_col_offset,
                                                         factory, param, scalar_key(literal.value)])
    return {"definitions": definitions, "sites": sites, "guards": guards}
