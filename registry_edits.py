"""Resolve structural changes in SQL dispatch tables to observed calls and key reads."""
import ast
import gzip
import json
import marshal
from functools import lru_cache
from pathlib import Path

import callback_consumers
import parser_table_keys
from replay_edit import preserve_scope

ROOT=Path(__file__).resolve().parent
PARSER_TABLES=parser_table_keys.TABLES
TABLES=PARSER_TABLES | {"TRANSFORMS"}


def declarations(tree):
    rows={}
    for cls in tree.body:
        if not isinstance(cls,ast.ClassDef):
            continue
        for node in cls.body:
            target=(node.targets[0] if isinstance(node,ast.Assign) and len(node.targets)==1 else
                    node.target if isinstance(node,ast.AnnAssign) else None)
            if isinstance(target,ast.Name) and target.id in TABLES and isinstance(node.value,ast.Dict):
                label=(cls.name,target.id)
                if label in rows:
                    raise ValueError("Repeated table declaration")
                rows[label]=node.value
    return rows


def key_form(key):
    return ast.dump(key) if key is not None else None


def unique_entries(table):
    result={}
    for i,(key,value) in enumerate(zip(table.keys,table.values)):
        if key is None:
            continue
        code=key_form(key)
        if code in result:
            raise ValueError("Repeated key")
        result[code]=(i,key,value)
    return result


@lru_cache(1)
def allowed_constructors():
    path=ROOT/"cache/dispatch-constructors"
    return set(marshal.loads(path.read_bytes())) if path.is_file() else set()


def callback(value):
    if isinstance(value,ast.Lambda):
        return not value.args.defaults and not any(value.args.kw_defaults)
    if isinstance(value,ast.Attribute) and value.attr=="from_arg_list" and isinstance(value.value,ast.Attribute):
        cls=value.value
        return isinstance(cls.value,ast.Name) and cls.value.id=="exp" and cls.attr in allowed_constructors()
    return False


def changes(old,new):
    aa,bb=ast.parse(old),ast.parse(new)
    left,right=declarations(aa),declarations(bb)
    if left.keys()!=right.keys():
        return
    results=[]
    for label,old_table in left.items():
        new_table=right[label]
        if ast.dump(old_table)==ast.dump(new_table):
            continue
        first,second=unique_entries(old_table),unique_entries(new_table)
        if label[1]=="TRANSFORMS" and [key_form(k) for k in old_table.keys] != [key_form(k) for k in new_table.keys]:
            return
        indices_a=set();indices_b=set()
        for key in first.keys() | second.keys():
            a,b=first.get(key),second.get(key)
            if a and b and ast.dump(a[2])==ast.dump(b[2]):
                continue
            literal=(a or b)[1]
            if label[1] in PARSER_TABLES:
                if not isinstance(literal,ast.Constant) or type(literal.value) is not str:
                    return
                if any(not callback(v[2]) for v in (a,b) if v):
                    return
                results.append((label,literal.value,None))
            else:
                if not a or not b or not isinstance(a[2],ast.Lambda) or not isinstance(b[2],ast.Lambda):
                    return
                if ast.dump(a[2].args)!=ast.dump(b[2].args):
                    return
                results.append((label,None,(a[2].lineno,a[2].col_offset)))
            if a:indices_a.add(a[0])
            if b:indices_b.add(b[0])
        for table,indices in ((old_table,indices_a),(new_table,indices_b)):
            table.keys=[x for i,x in enumerate(table.keys) if i not in indices]
            table.values=[x for i,x in enumerate(table.values) if i not in indices]
    if results and ast.dump(aa)==ast.dump(bb):
        return results


def expand(selector, contexts):
    out=set()
    for name in contexts:
        if name in selector.NUM:out.add(selector.NUM[name])
        elif name.startswith("scope:") and name[6:] in selector.BY_FILE:out.update(selector.BY_FILE[name[6:]])
        else:return selector.ALL
    return out


def compute(selector,patch):
    path=patch["old"]
    if (not path or patch["new"]!=path or not path.startswith(("sqlglot/parsers/","sqlglot/generators/"))
            or path not in selector.SRC or path in selector.profile()["source_reads"]):
        return
    original=selector.SRC[path]
    updated,removed,added=selector.apply(original,patch["hunks"])
    if not removed or len(removed)+len(added)>140:
        return
    spans=callback_consumers.index(selector,path)
    if not any(n in spans for n in removed):return
    delta=changes(original,updated)
    if not delta:return
    data,sites=parser_table_keys.profile()
    covered,entries,readers=selector.dispatch_map()
    if not covered or data!=set(selector.NUM):return
    keep=set(selector.UNTRACED_POOL_TESTS)
    keep.update(selector.tests_of(selector.M["opens"].get(path)))
    keep.update(n for n,(raw,_) in selector.EXTRA.items() if path in raw)
    mask=sum(1<<n for n in set(selector.profile()["code_reads"].get(path,())))
    if mask:keep.update(preserve_scope(selector,path,mask,selector.ALL))
    for (owner,table),key,position in delta:
        if position:
            site=f"{path}:{position[0]}:{position[1]}"
            if site not in entries:return
            keep.update(expand(selector,readers.get(site,())))
        else:
            stack=[path[:-3].replace("/",".")+":"+owner+":"+table]
            seen=set()
            while stack:
                site=stack.pop()
                if site in seen:continue
                seen.add(site)
                if site not in sites:return
                row=sites[site]
                stack.extend(row.get("@copy",()))
                keep.update(expand(selector,(*row.get(key,()),*row.get("*",()))))
    return keep


def refine(selector,text,selected):
    if not selected:return selected
    patches=selector.parse_patch(text)
    if len(patches)!=1:return selected
    try:
        got=compute(selector,patches[0])
    except (ValueError,SyntaxError,RecursionError):
        return selected
    return got if got is not None else selected
