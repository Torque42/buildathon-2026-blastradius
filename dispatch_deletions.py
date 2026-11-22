"""Select consumers when a generator loses only inert entries from class-keyed maps."""
import ast
from functools import lru_cache


FIELDS={"TRANSFORMS","PROPERTIES_LOCATION"}


def slots(tree):
    for owner in tree.body:
        if not isinstance(owner,ast.ClassDef):continue
        for stmt in owner.body:
            target=(stmt.target if isinstance(stmt,ast.AnnAssign) else stmt.targets[0]
                    if isinstance(stmt,ast.Assign) and len(stmt.targets)==1 else None)
            if isinstance(target,ast.Name) and target.id in FIELDS and isinstance(stmt.value,ast.Dict):
                yield (owner.name,target.id),stmt.value


def class_key(node):
    if isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name) and node.value.id=="exp":
        return node.attr


def inert(value,field):
    if field=="TRANSFORMS":
        return isinstance(value,ast.Lambda) and not value.args.defaults and not any(value.args.kw_defaults)
    return (isinstance(value,ast.Attribute) and isinstance(value.value,ast.Attribute)
            and value.value.attr=="Location" and class_key(value.value.value)=="Properties")


def removed(old,new):
    left=ast.parse(old);right=ast.parse(new)
    first=dict(slots(left));second=dict(slots(right))
    if first.keys()!=second.keys():return
    names=set()
    for owner,table in first.items():
        peer=second[owner]
        remaining={class_key(k) for k in peer.keys}
        remove=set()
        for i,(key,value) in enumerate(zip(table.keys,table.values)):
            name=class_key(key)
            if name and name not in remaining:
                if not inert(value,owner[1]):return
                remove.add(i);names.add(name)
        table.keys=[k for i,k in enumerate(table.keys) if i not in remove]
        table.values=[v for i,v in enumerate(table.values) if i not in remove]
    return names if names and ast.dump(left)==ast.dump(right) else None


@lru_cache(1)
def reflection(s):
    # Direct tests of the generator's tables, and test modules that reflect over
    # objects or namespaces, can observe a missing key without creating its AST.
    terms=("TRANSFORMS","PROPERTIES_LOCATION","_dispatch","__dict__","getmembers(","vars(","dir(")
    keep=set()
    for file in s.SRC:
        if not file.startswith("tests/") or not file.endswith(".py"):continue
        if not any(term in s.SRC[file] for term in terms):continue
        keep.update(s.BY_FILE.get(file,()))
        keep.update(s.dependent_test_numbers(file))
    return keep


def readers(s,path,updated,cls_readers):
    if path!="sqlglot/generator.py" and not path.startswith("sqlglot/generators/"):return
    if not cls_readers:return
    names=removed(s.SRC[path],updated)
    if not names or not names<=cls_readers.keys():return
    # The consumer certificate for the coordinated deletion also rules out
    # lingering references and retains open, opaque and reflective class reads.
    keep=reflection(s).copy()
    for name in names:keep.update(cls_readers[name])
    keep.update(s.tests_of(s.M["opens"].get(path)))
    keep.update(n for n,(raw,_) in s.EXTRA.items() if path in raw)
    from replay_edit import preserve_scope
    keep.update(preserve_scope(s,path,sum(1<<line for line in set(s.profile()["code_reads"].get(path,()))),s.ALL))
    return keep
