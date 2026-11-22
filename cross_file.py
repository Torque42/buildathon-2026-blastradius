"""Follow the first observable change across multiple modules of the pinned project."""
import ast
import gzip
import marshal
import sys
from functools import lru_cache
from pathlib import Path

import function_changes
import instruction_impact
import replay_edit
import dispatch_deletions


@lru_cache(1)
def exports():
    path=Path(__file__).parent/"maps/export-consumers.gz"
    if not path.is_file():
        return {},{}
    names,events=marshal.loads(gzip.decompress(path.read_bytes()))
    return {name:1<<i for i,name in enumerate(names)},events


def protected(s,path,line_mask):
    result=s.tests_of(s.M["opens"].get(path)) | s.UNTRACED_POOL_TESTS
    result.update(n for n,(raw,_) in s.EXTRA.items() if path in raw)
    result.update(replay_edit.preserve_scope(s,path,line_mask,s.ALL))
    return result


def body_readers(s,path,source):
    old=s.SRC[path]
    shell,funcs,codes=function_changes.baseline(path,old)
    after,others=function_changes.skeleton(source)
    if shell!=after or funcs.keys()!=others.keys() or None in funcs.values() or None in others.values():
        return
    changed=[n for n in funcs if funcs[n][1]!=others[n][1]]
    if not 1<=len(changed)<=8:return
    version,bits,known,files=instruction_impact.observations()
    if tuple(sys.version_info[:2])!=tuple(version) or path not in files:return
    updated={}
    for _,c in instruction_impact.lineage(compile(source,path,"exec",dont_inherit=True)):
        updated.setdefault(c.co_qualname,[]).append(c)
    init=s.collection_guard.inputs()[1].get(path,0)
    init|=sum(1<<n for n in s.import_impact.profile()[0].get(path,{}))
    mask=may=0
    for name in changed:
        if len(codes.get(name,()))!=1 or len(updated.get(name,()))!=1:return
        top=codes[name][0]
        peer=updated[name][0]
        if top.co_flags!=peer.co_flags:return
        for c in function_changes.members(top):
            peers=updated.get(c.co_qualname,())
            wanted=instruction_impact.offset_mask(c,peers[0]) if len(peers)==1 else None
            if wanted==0:continue
            spans=sum(1<<n for n in {n for _,_,n in c.co_lines() if n is not None})
            mask|=spans
            if spans&init:
                # A call made while importing can put its result in a module global.
                # Require reachability beyond the definition line itself.
                if (spans & ~(1<<c.co_firstlineno)) & init:return
                if c.co_firstlineno in s.import_impact.profile()[0].get(path,{}):return
            for (label,first,digest),groups in files[path].items():
                if label!=c.co_qualname or first!=c.co_firstlineno:continue
                import hashlib
                same=hashlib.blake2s(c.co_code,digest_size=12).digest()==digest
                for visits,tests in groups.items():
                    if not same or wanted is None or visits&wanted:may|=tests
    return protected(s,path,mask) | {n for n in s.ALL if not (bits.get(s.TEST_FILE_ID[n],0)&known) or bits.get(s.TEST_FILE_ID[n],0)&may}


def removed_types(s,path,source):
    if not path.startswith("sqlglot/expressions/"):return
    tree=ast.parse(s.SRC[path]);after=ast.parse(source)
    survivors={n.name for n in after.body if isinstance(n,ast.ClassDef)}
    removed=[n for n in tree.body if isinstance(n,ast.ClassDef) and n.name not in survivors]
    if not removed:return
    tree.body=[n for n in tree.body if n not in removed]
    if ast.dump(tree)!=ast.dump(after):return
    for cls in removed:
        if (cls.name in s.BASE_NAMES or cls.decorator_list or cls.keywords or len(cls.bases)!=1 or
                not isinstance(cls.bases[0],ast.Name) or cls.bases[0].id not in {"Property","Expression"} or
                len(cls.body)!=1 or not isinstance(cls.body[0],ast.Assign)):
            return
        stmt=cls.body[0]
        if (len(stmt.targets)!=1 or not isinstance(stmt.targets[0],ast.Name) or stmt.targets[0].id!="arg_types"
                or not isinstance(stmt.value,ast.Dict) or not all(isinstance(n,ast.Constant) and
                    type(n.value) is str for n in stmt.value.keys) or not all(isinstance(n,ast.Constant) and
                    type(n.value) is bool for n in stmt.value.values)):
            return
    return removed


def removed_table_keys(s,edits,types):
    permitted=set()
    for path,(new,_,_) in edits.items():
        if not (path=="sqlglot/generator.py" or path.startswith("sqlglot/generators/")):continue
        for table in ast.walk(s.source_tree(path)):
            target=table.target if isinstance(table,ast.AnnAssign) else (
                table.targets[0] if isinstance(table,ast.Assign) and len(table.targets)==1 else None)
            if not isinstance(target,ast.Name) or target.id not in {"TRANSFORMS","PROPERTIES_LOCATION"}:continue
            if not isinstance(table.value,ast.Dict):continue
            for key in table.value.keys:
                if (isinstance(key,ast.Attribute) and isinstance(key.value,ast.Name) and key.value.id=="exp"
                        and key.attr in types and key.attr not in new):
                    permitted.add((key.attr,path,key.lineno))
    return permitted


def type_readers(s,path,removed,edits):
    bits,events=exports()
    if set(bits)!=set(s.NUM) or "@installed" not in events:return
    names={cls.name for cls in removed}
    permitted=removed_table_keys(s,edits,names)
    lines={n for cls in removed for n in range(cls.lineno,cls.end_lineno+1)}
    keep=protected(s,path,sum(1<<n for n in lines));mask=0
    for name in names:
        if name not in events:return
        for key,tests in events[name].items():
            if key[0]=="read":mask|=tests
            elif key[0]=="init":
                _,file,line,_=key
                if (name,file,line) not in permitted:return
            else:return
        for test in s.BY_FILE:
            if name in s.SRC.get(test,""):
                keep.update(s.BY_FILE[test]);keep.update(s.dependent_test_numbers(test))
    for key,tests in events.get("*",{}).items():
        if key[0]=="read":mask|=tests
        elif key[0]=="init":return
    # A literal use left in a production module can run during an import that
    # the test suite loads lazily. Preserve the original fallback in that case.
    for file,old in s.SRC.items():
        if file.startswith("sqlglot/") and file.endswith(".py"):
            new=edits[file][0] if file in edits else old
            if any(name in new for name in names):return
    return keep|{n for n in s.ALL if bits[s.TEST_FILE_ID[n]]&mask}


def refine(s,text,selected):
    if not selected:return selected
    parts=s.parse_patch(text)
    if not 2<=len(parts)<=6:return selected
    edits={}
    for p in parts:
        path=p["old"]
        if (not path or not path.startswith("sqlglot/") or not path.endswith(".py") or
                path!=p["new"] or path not in s.SRC or path in edits or path in s.profile()["source_reads"]):
            return selected
        edits[path]=s.apply(s.SRC[path],p["hunks"])
    by_file={};cls_readers={}
    for path,(source,_,_) in edits.items():
        removed=removed_types(s,path,source)
        if not removed:continue
        result=type_readers(s,path,removed,edits)
        if result is not None:
            by_file[path]=result
            cls_readers.update((node.name,result) for node in removed)
    union=set();refined=False
    for p in parts:
        path=p["old"];source=edits[path][0]
        keep=by_file.get(path)
        if keep is None:
            keep=dispatch_deletions.readers(s,path,source,cls_readers)
        if keep is None:
            keep=body_readers(s,path,source)
        if keep is None:
            reasons=[]
            keep=s.analyze_py_source(path,p,reasons,False)|s.tests_of(s.M["opens"].get(path))
            if reasons!=["comment-only"]:
                keep|=s.extra_tests(path,reasons)|s.UNTRACED_POOL_TESTS
            if path.startswith("sqlglot/typing/"):keep.update(s.BY_FILE.get("tests/test_typing.py",()))
        else:refined=True
        union.update(keep)
        if selected<=union:return selected
    return selected & union if refined else selected
