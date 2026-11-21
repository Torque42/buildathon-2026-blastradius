"""Certify identity-to-equality changes from the actual operand protocols."""
import gzip
import marshal
from functools import lru_cache
from pathlib import Path

import identity_select
from replay_edit import preserve_scope

@lru_cache(1)
def evidence():
    path=Path(__file__).parent/"maps/equality-readers.gz"
    catalog, contexts, rows=marshal.loads(gzip.decompress(path.read_bytes()))
    return set(catalog), contexts, rows

def match(selector,text):
    if " is " not in text or not ("==" in text or "!=" in text):
        return None
    files=selector.parse_patch(text)
    if len(files)!=1:
        return None
    patch=files[0]
    path=patch["old"]
    if (not path or path!=patch["new"] or not path.startswith("sqlglot/") or
            path not in selector.SRC or path in selector.profile()["source_reads"]):
        return None
    old=selector.SRC[path]
    new,removed,added=selector.apply(old,patch["hunks"])
    if len(removed)!=1 or len(added)!=1:
        return None
    line=removed[0]
    if line not in selector.FACTS.get("statements",{}).get(path,{}):
        return None
    for start,end,name in selector.FACTS.get("functions",{}).get(path,()):
        if start<=line<=end and name.rsplit(".",1)[-1] in {"__eq__","__ne__","__getattribute__"}:
            return None
    a,b=old.splitlines(),new.splitlines()
    if a[:line-1]!=b[:line-1] or a[line:]!=b[line:]:
        return None
    left,right=a[line-1],b[line-1]
    if not left.isascii() or not right.isascii():
        return None
    for start,end,operator,site,_ in identity_select.locations(selector,path).get(line,()):
        suffix=left[end:]
        if not right.startswith(left[:start]) or not right.endswith(suffix):
            continue
        if right[start:len(right)-len(suffix)].strip()!=operator:
            continue
        catalog, contexts, rows=evidence()
        if catalog!=set(selector.NUM) or site not in rows:
            return None
        mask=rows[site]
        keep=selector.tests_of(selector.M["opens"].get(path))|selector.UNTRACED_POOL_TESTS
        keep.update(n for n,(opened,_) in selector.EXTRA.items() if path in opened)
        keep.update(preserve_scope(selector,path,1<<line,selector.ALL))
        while mask:
            bit=mask&-mask
            mask-=bit
            ctx=contexts[bit.bit_length()-1]
            if ctx in selector.NUM:
                keep.add(selector.NUM[ctx])
            elif ctx.startswith("scope:") and ctx[6:] in selector.BY_FILE:
                keep.update(selector.BY_FILE[ctx[6:]])
            else:
                return None
        return keep
    return None
