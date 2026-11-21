import ast
import gzip
import json
import marshal
from pathlib import Path
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
cache = root / "cache"
cache.mkdir(exist_ok=True)
for name in ("maps", "sources", "pool", "observed-attributes", "source-index", "extra-lines"):
    with (cache / name).open("wb") as stream:
        marshal.dump(json.load(gzip.open(root / "maps" / (name + ".json.gz"))), stream)
with tempfile.TemporaryDirectory() as directory:
    (Path(directory) / "tests.json").write_text('{"tests":[]}')
    sys.argv = [sys.argv[0], directory]
    sys.path.insert(0, str(root))
    import selector

queries = set()
functions, statements, comments, fields, interiors, callbacks = {}, {}, {}, {}, {}, {}
for path, source in selector.SRC.items():
    if not path.startswith("sqlglot/") or not path.endswith(".py"):
        continue
    tree = ast.parse(source)
    if path.startswith(("sqlglot/parsers/","sqlglot/generators/")):
        callbacks[path] = selector.callback_spans(path,tree,source)
    lines = source.splitlines()
    locations = fields[path] = {}
    def fact(node, start, end, kind, detail):
        if node.lineno == node.end_lineno and lines[node.lineno-1].isascii():
            locations.setdefault(node.lineno, []).append((start,end,kind,detail))
    hierarchy = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    functions[path] = selector.function_spans(tree)
    locator = selector.Locator(source, tree)
    indexed = {}
    for start,end,node,parents in sorted(locator.stmts,key=lambda row: row[1]-row[0],reverse=True):
        fn = selector.in_function(parents)
        for line in range(start,end+1):
            indexed.pop(line,None)
            if fn is None or isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef,ast.ClassDef)):
                continue
            if isinstance(node,(ast.If,ast.For,ast.While,ast.With,ast.Try,ast.AsyncFor,ast.AsyncWith)):
                last = selector.header_end(node)
                if line <= last:
                    indexed[line] = (start,last,"compound-header")
            else:
                indexed[line] = (start,end,"stmt")
    statements[path] = indexed
    import tokenize, io
    protected = set()
    interior = interiors[path] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.end[0] > token.start[0] and token.type not in (tokenize.NL,tokenize.NEWLINE):
            protected.update(range(token.start[0], token.end[0]+1))
            interior.update(range(token.start[0]+1, token.end[0]+1))
    comments[path] = protected
    for node in ast.walk(tree):
        if isinstance(node,(ast.JoinedStr,ast.Constant)) and node.lineno != node.end_lineno:
            protected.update(range(node.lineno,node.end_lineno+1))
            interior.update(range(node.lineno+1,node.end_lineno+1))
        if isinstance(node,ast.Constant) and type(node.value) is int:
            up=hierarchy.get(node)
            if isinstance(up,ast.Tuple) and isinstance(hierarchy.get(up),ast.Dict):
                dictionary=hierarchy[up]
                statement=hierarchy.get(dictionary)
                owner=hierarchy.get(statement)
                outer=hierarchy.get(owner)
                if isinstance(owner,ast.ClassDef) and isinstance(outer,ast.ClassDef) and isinstance(hierarchy.get(outer),ast.Module) and isinstance(statement,ast.Assign) and len(statement.targets)==1 and isinstance(statement.targets[0],ast.Name) and statement.targets[0].id=="NUMERIC_ESCAPES" and up in dictionary.values and all(isinstance(k,ast.Constant) and type(k.value) is str for k in dictionary.keys) and all(isinstance(v,ast.Tuple) and len(v.elts)==4 and all(isinstance(x,ast.Constant) and type(x.value) is int for x in v.elts) for v in dictionary.values):
                    key=dictionary.keys[dictionary.values.index(up)].value
                    fact(node,node.col_offset,node.end_col_offset,"key",(path[:-3].replace("/",".")+":"+outer.name+"."+owner.name+":NUMERIC_ESCAPES",key))
            ancestor=node
            lambdas=[]
            while ancestor in hierarchy and not isinstance(ancestor,ast.stmt):
                if isinstance(ancestor,ast.Lambda):
                    lambdas.append((ancestor.lineno,ancestor.end_lineno))
                ancestor=hierarchy[ancestor]
            owner=hierarchy.get(ancestor)
            if isinstance(ancestor,ast.Assign) and len(ancestor.targets)==1 and isinstance(ancestor.targets[0],ast.Name) and isinstance(owner,ast.ClassDef) and isinstance(hierarchy.get(owner),ast.Module):
                if isinstance(ancestor.value,ast.UnaryOp) and ancestor.value.operand is node or ancestor.value is node or lambdas and all(node.lineno>lo for lo,hi in lambdas):
                    if not lambdas or len(lambdas)==1 and sum(isinstance(obj,ast.Lambda) and obj.lineno<=node.lineno<=obj.end_lineno for obj in ast.walk(ancestor))==1:
                        fact(node,node.col_offset,node.end_col_offset,"class-int",(owner.name,ancestor.targets[0].id,lambdas))
        if isinstance(node, ast.Constant) and type(node.value) is bool:
            parent = hierarchy.get(node)
            if isinstance(parent,ast.Dict) and node in parent.values:
                statement = hierarchy.get(parent)
                cls = hierarchy.get(statement)
                if isinstance(statement,ast.Assign) and isinstance(cls,ast.ClassDef) and path.startswith("sqlglot/expressions/") and len(statement.targets)==1 and isinstance(statement.targets[0],ast.Name) and statement.targets[0].id == "arg_types" and cls.name not in selector.BASE_NAMES and not cls.decorator_list and len(cls.bases)==1 and isinstance(cls.bases[0],ast.Name) and cls.bases[0].id=="Expression" and len(cls.body)==1:
                    key = parent.keys[parent.values.index(node)]
                    if isinstance(key,ast.Constant) and type(key.value) is str:
                        fact(node,node.col_offset,node.end_col_offset,"required",(path[:-3].replace("/",".")+":"+cls.name,key.value))
            elif isinstance(parent,ast.keyword):
                call = hierarchy.get(parent)
                if isinstance(call, ast.Call) and parent.arg=="copy" and ast.unparse(call.func)=="exp.alias_" and len(call.args)==1 and isinstance(call.args[0],ast.Call) and ast.unparse(call.args[0].func)=="exp.table_" and len(call.args[0].args)==1 and not call.args[0].keywords and {kw.arg for kw in call.keywords}=={"alias","copy"} and len(call.keywords)==2 and "from sqlglot import expressions as exp" in source:
                    fact(node,node.col_offset,node.end_col_offset,"owned",f"{path}:{call.lineno}:{call.col_offset}")
                if isinstance(call,ast.Call) and isinstance(hierarchy.get(call),ast.Dict) and path.startswith("sqlglot/parsers/"):
                    dictionary = hierarchy[call]
                    statement = hierarchy.get(dictionary)
                    if isinstance(statement,ast.Assign) and len(statement.targets)==1 and isinstance(statement.targets[0],ast.Name) and statement.targets[0].id=="SHOW_PARSERS" and isinstance(hierarchy.get(statement),ast.ClassDef) and call in dictionary.values and selector.factory_arguments(call):
                        site=selector.pure_factory(path,call.func.id)
                        if site:
                            fact(node,node.col_offset,node.end_col_offset,"factory",(site,json.dumps([site,*selector.factory_arguments(call)],sort_keys=True)))
        if isinstance(node,ast.Compare) and len(node.ops)==1 and len(node.comparators)==1 and node.left.end_lineno==node.comparators[0].lineno==node.lineno:
            left,right=node.left,node.comparators[0]
            if isinstance(node.ops[0],(ast.Lt,ast.LtE,ast.Gt,ast.GtE)) and isinstance(left,ast.Attribute) and left.attr=="version" and selector.self_field(left.value,"dialect") and isinstance(right,ast.Tuple) and len(right.elts)!=3:
                fact(node,left.end_col_offset,right.col_offset,"order",f"{path}:{node.lineno}:{node.col_offset}")
            if isinstance(node.ops[0],(ast.Is,ast.IsNot)):
                field,other=(left,right) if selector.self_field(left) else (right,left)
                if selector.self_field(field):
                    if isinstance(other,ast.Attribute) and isinstance(other.value,ast.Name):
                        enum=other.value.id
                        if selector.simple_enum(path,enum):
                            _,decl=selector.imported_class(path,enum)
                            if any(isinstance(st,ast.Assign) and any(isinstance(t,ast.Name) and t.id==other.attr for t in st.targets) for st in decl.body):
                                fact(node,left.end_col_offset,right.col_offset,"enum",(field.attr,enum))
                    elif isinstance(other,ast.Constant) and other.value is None:
                        owner=hierarchy.get(node)
                        while owner is not None and not isinstance(owner,ast.ClassDef):
                            owner=hierarchy.get(owner)
                        if owner is not None and selector.optional_identity_field(path,field.attr,owner):
                            fact(node,left.end_col_offset,right.col_offset,"identity",None)
        if not isinstance(node, ast.Compare) or len(node.ops) != 1 or not isinstance(node.ops[0], (ast.Is, ast.IsNot)):
            continue
        for field, enum in ((node.left, node.comparators[0]), (node.comparators[0], node.left)):
            if selector.self_field(field) and isinstance(enum, ast.Attribute) and isinstance(enum.value, ast.Name):
                queries.add((field.attr, enum.value.id))
with (cache / "facts").open("wb") as stream:
    marshal.dump({"dispatch_spans":callbacks,"test_importers": selector.test_module_imports(), "pool": selector.POOL_IDS, "fields":fields, "functions":functions, "statements":statements, "multiline":comments, "interior":interiors, "version": selector.fixed_dialect_versions(), "enum": {pair: selector.enum_attribute(*pair) for pair in queries}}, stream)
