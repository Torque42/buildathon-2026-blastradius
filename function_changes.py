"""Use cold instruction coverage for replacements and insertions inside existing functions."""
import ast
import hashlib
import sys
from functools import lru_cache
from types import CodeType

import instruction_impact as impact
from replay_edit import preserve_scope

def skeleton(source):
    tree=ast.parse(source)
    bodies={}
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
                    bodies[key]=(node.lineno,ast.dump(ast.Module(body=body,type_ignores=[])))
    stage(tree.body,())
    return ast.dump(tree),bodies

@lru_cache(18)
def baseline(path,source):
    shell,functions=skeleton(source)
    codes={}
    for _,c in impact.lineage(compile(source,path,"exec",dont_inherit=True)):
        codes.setdefault(c.co_qualname,[]).append(c)
    return shell,functions,codes

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
    shell,others=skeleton(new)
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
