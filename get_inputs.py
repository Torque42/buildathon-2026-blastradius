"""Match a changed dict.get argument to tests that can observe a different result."""
import ast
from functools import lru_cache
import gzip
import marshal
from pathlib import Path

from replay_edit import preserve_scope


@lru_cache(maxsize=1)
def profile():
    catalog,contexts,records=marshal.loads(gzip.decompress((Path(__file__).parent/"maps/get-reads.gz").read_bytes()))
    return set(catalog),contexts,records


@lru_cache(maxsize=32)
def positions(selector,path):
    index={}
    for n in ast.walk(selector.source_tree(path)):
        if (not isinstance(n,ast.Call) or not isinstance(n.func,ast.Attribute) or n.func.attr!="get"
                or n.keywords or len(n.args) not in (1,2) or any(isinstance(x,ast.Starred) for x in n.args)
                or not isinstance(n.args[0],ast.Constant) or type(n.args[0].value) is not str):
            continue
        for pos,arg in enumerate(n.args):
            if isinstance(arg,ast.Constant) and arg.lineno==arg.end_lineno:
                index.setdefault(arg.lineno,[]).append((arg.col_offset,arg.end_col_offset,(path,n.lineno,n.col_offset),pos,type(arg.value)))
    return index


def refine(selector,text,selected):
    if not selected or ".get(" not in text:
        return selected
    patches=selector.parse_patch(text)
    if len(patches)!=1:return selected
    patch=patches[0];path=patch["old"]
    if not path or path!=patch["new"] or path not in selector.SRC or not path.startswith("sqlglot/"):
        return selected
    if path in selector.profile()["source_reads"]:return selected
    original=selector.SRC[path]
    updated,removed,added=selector.apply(original,patch["hunks"])
    if len(removed)!=1 or len(added)!=1:return selected
    line=removed[0];a=original.splitlines();b=updated.splitlines()
    if a[:line-1]!=b[:line-1] or a[line:]!=b[line:] or not a[line-1].isascii() or not b[line-1].isascii():
        return selected
    for start,end,site,pos,typ in positions(selector,path).get(line,()):
        prefix,suffix=a[line-1][:start],a[line-1][end:]
        if not b[line-1].startswith(prefix) or not b[line-1].endswith(suffix):continue
        stop=len(b[line-1])-len(suffix)
        try:
            value=ast.parse(b[line-1][start:stop],mode="eval").body
        except (SyntaxError,ValueError):
            continue
        if not isinstance(value,ast.Constant) or type(value.value) is not typ:continue
        catalog,contexts,records=profile()
        if catalog!=set(selector.NUM) or site not in records:return selected
        unknown,missing,closed,denied,allowed=records[site]
        affected=unknown | (missing if pos else (closed & ~allowed.get(value.value,0)) | denied.get(value.value,0))
        keep=selector.tests_of(selector.M["opens"].get(path)) | selector.UNTRACED_POOL_TESTS
        keep.update(n for n,(raw,_) in selector.EXTRA.items() if path in raw)
        keep.update(preserve_scope(selector,path,1<<line,selector.ALL))
        while affected:
            bit=affected & -affected; affected-=bit
            context=contexts[bit.bit_length()-1]
            if context in selector.NUM:keep.add(selector.NUM[context])
            elif context.startswith("scope:") and context[6:] in selector.BY_FILE:keep.update(selector.BY_FILE[context[6:]])
            else:return selected
        return selected & keep
    return selected
