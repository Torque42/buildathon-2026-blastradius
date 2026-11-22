"""Use cold instruction coverage for replacements and insertions inside existing functions."""
import ast
import hashlib
import marshal
from pathlib import Path
import sys
from functools import lru_cache
from types import CodeType

import instruction_impact as impact
from replay_edit import preserve_scope

SHAPES = Path(__file__).resolve().parent / "cache/function-shapes"


def skeleton(source, previous=None, collect=None):
    tree=ast.parse(source)
    bodies={}
    lines=source.splitlines(keepends=True)
    # Restrict edits to functions declared directly in module or class bodies.
    # Database initialization, decorators, signatures and class-definition
    # side effects consequently retain their original execution order.
    def stage(stmts,prefix):
        for node in stmts:
            if isinstance(node,ast.ClassDef):
                stage(node.body,(*prefix,node.name))
            elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)):
                key=".".join((*prefix,node.name))
                if key in bodies:
                    bodies[key]=None
                else:
                    if node.body and isinstance(node.body[0],ast.Expr) and isinstance(node.body[0].value,ast.Constant) and type(node.body[0].value.value) is str:
                        body=node.body[1:];node.body=node.body[:1]
                    else:
                        body=node.body;node.body=[]
                    if body:
                        start = min([body[0].lineno] + [d.lineno for d in getattr(body[0], "decorator_list", ())])
                        end = body[-1].end_lineno
                        raw = "".join(lines[start-1:end]).encode()
                        boundary = (body[0].col_offset, body[-1].end_col_offset)
                        token = key, boundary, hashlib.sha256(raw).digest()
                    else:
                        token = key, (), b""
                    value = previous.get(token) if previous else None
                    if value is None:
                        representation = ast.dump(ast.Module(body=body,type_ignores=[])).encode()
                        value = hashlib.sha256(representation).digest()
                    if collect is not None:
                        collect[token] = value
                    bodies[key]=(node.lineno,value)
    stage(tree.body,())
    return hashlib.sha256(ast.dump(tree).encode()).digest(),bodies


def make_baseline(path, source):
    tokens = {}
    shell, functions = skeleton(source, collect=tokens)
    codes = {}
    for _, code in impact.lineage(compile(source, path, "exec", dont_inherit=True)):
        codes.setdefault(code.co_qualname, []).append(code)
    return shell, functions, codes, tokens


@lru_cache(32)
def pinned(path, source):
    if path.startswith("sqlglot/") and ".." not in Path(path).parts:
        item = SHAPES / (path + ".marshal")
        if item.is_file():
            digest, version, result = marshal.loads(item.read_bytes())
            if tuple(version) == sys.version_info[:2] and digest == hashlib.sha256(source.encode()).digest():
                return result
    return make_baseline(path, source)

@lru_cache(18)
def baseline(path,source):
    return pinned(path,source)[:3]


@lru_cache(6)
def changed_skeleton(path, before, after):
    return skeleton(after, previous=pinned(path,before)[3])

def members(code):
    yield code
    for item in code.co_consts:
        if isinstance(item,CodeType):yield from members(item)

def mask_changes(selector,patch):
    path=patch["old"]
    if path!=patch["new"] or not path or path not in selector.SRC or not path.startswith("sqlglot/"):return
    old=selector.SRC[path]
    new,removed,added=selector.apply(old,patch["hunks"])
    if not removed and not added or len(removed)+len(added)>240 or path in selector.profile()["source_reads"]:return
    if len(removed)==len(added) and len(removed)<=10:return
    version,bits,known,files=impact.observations()
    if tuple(sys.version_info[:2])!=tuple(version) or path not in files:return
    base,funcs,codes=baseline(path,old)
    shell,others=changed_skeleton(path,old,new)
    if shell!=base or funcs.keys()!=others.keys():return
    targets=[k for k in funcs if funcs[k] and others[k] and funcs[k][1]!=others[k][1]]
    if not targets or len(targets)>10:return
    updated={}
    for _,c in impact.lineage(compile(new,path,"exec",dont_inherit=True)):
        updated.setdefault(c.co_qualname,[]).append(c)
    records=files[path]
    may=0
    for key in targets:
        if len(codes.get(key,()))!=1 or len(updated.get(key,()))!=1:return
        before=codes[key][0];after=updated[key][0]
        old_members=list(members(before));new_members=list(members(after))
        direct=len(old_members)==len(new_members) and all(l.co_qualname==r.co_qualname for l,r in zip(old_members,new_members))
        for idx,member in enumerate(old_members):
            wanted=None
            if direct:
                peer=new_members[idx]
                wanted=impact.offset_mask(member,peer)
                # Generator flags, argument metadata or code object introspection
                # may change without executing a body; metadata readers are added below.
                if wanted==0:continue
            name,lineno=member.co_qualname,member.co_firstlineno
            canonical=hashlib.blake2s(member.co_code,digest_size=12).digest()
            for (label,first,digest),groups in records.items():
                if label!=name or first!=lineno:continue
                for offsets,nodes in groups.items():
                    if digest!=canonical or wanted is None or offsets&wanted:may|=nodes
    return path,known & ~may

def refine(selector,text,selected):
    if not selected:return selected
    patches=selector.parse_patch(text)
    if len(patches)!=1:return selected
    patch=patches[0];path=patch["old"]
    # Compiler tracing is more costly than the earlier one-line filters.
    if not path or not patch["hunks"] or all(len([x for x in h["lines"] if x.startswith("-")])==len([x for x in h["lines"] if x.startswith("+")]) and len(h["lines"])<20 for h in patch["hunks"]):return selected
    result=mask_changes(selector,patch)
    if result is None:return selected
    path,safe=result
    _,bits,_,_=impact.observations()
    readers=selector.tests_of(selector.M["opens"].get(path))
    readers.update(n for n,(raw,_) in selector.EXTRA.items() if path in raw)
    lines=selector.profile()["code_reads"].get(path,())
    if lines:readers.update(preserve_scope(selector,path,sum(1<<n for n in set(lines)),selected))
    return {n for n in selected if n in readers or not safe&bits.get(selector.TEST_FILE_ID[n],0)}
