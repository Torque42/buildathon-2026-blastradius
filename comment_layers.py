"""Separate inert comment edits so a behavioral hunk can use its normal analysis."""
import ast
import difflib
import re
from replay_edit import preserve_scope

COMMENT=re.compile(r"^[ \t]*#")
DIRECTIVE=re.compile(r"coding[:=]|^\s*#\s*(?:!|type\s*:|pragma\b|fmt\b|noqa\b|pytest\b|cython\b|mypy\b)",re.I)
def simplify(selector,text):
    if not re.search(r"(?m)^[-+][ \t]*#",text):return text,set()
    # A single comment-only diff is already handled by the fast path.
    if not any(line.startswith(("+","-")) and not line.startswith(("+++","---")) and line[1:].strip() and not COMMENT.match(line[1:]) for line in text.splitlines()):return text,set()
    parts=re.split(r"(?m)(?=^diff --git )",text)
    out=[];keep=set();changed=False
    for part in parts:
        if not part:continue
        edits=selector.parse_patch(part)
        if len(edits)!=1:return text,set()
        patch=edits[0];path=patch["old"]
        if not path or path!=patch["new"] or path not in selector.SRC or not path.startswith("sqlglot/") or not re.search(r"(?m)^[-+][ \t]*#",part):
            out.append(part);continue
        base=selector.SRC[path].replace("\r\n","\n")
        full,_,_=selector.apply(base,patch["hunks"])
        hunks=[]
        for h in patch["hunks"]:
            ls=[]
            for line in h["lines"]:
                if line.startswith(("+","-")) and COMMENT.match(line[1:]) and not DIRECTIVE.search(line[1:]):
                    if line[0]=="-":ls.append(" "+line[1:])
                else:ls.append(line)
            hunks.append(dict(h,lines=ls))
        reduced,_,_=selector.apply(base,hunks)
        if reduced==full:out.append(part);continue
        try:same=ast.dump(ast.parse(reduced,type_comments=True))==ast.dump(ast.parse(full,type_comments=True))
        except (SyntaxError,ValueError):same=False
        if not same:out.append(part);continue
        # Source readers can see the original patch even when Python's syntax tree agrees.
        raw=selector.profile()["source_reads"]
        if path in raw:return text,set()
        keep.update(selector.tests_of(selector.M["opens"].get(path)))
        keep.update(i for i,(files,_) in selector.EXTRA.items() if path in files)
        lines=selector.profile()["code_reads"].get(path,())
        if lines:keep.update(preserve_scope(selector,path,sum(1<<n for n in set(lines)),selector.ALL))
        changed=True
        if reduced!=base:
            diff=difflib.unified_diff(base.splitlines(True),reduced.splitlines(True),"a/"+path,"b/"+path)
            out.append("diff --git a/"+path+" b/"+path+"\n"+"".join(diff))
    return ("".join(out),keep) if changed else (text,set())
