"""Keep the tests that consumed a changed slot value or tokenizer keyword."""
import ast
import gzip
import marshal
from functools import lru_cache
from pathlib import Path

from consumer_sites import scan, WORD
from replay_edit import preserve_scope

ROOT=Path(__file__).resolve().parent
@lru_cache(1)
def observations():
    names,slots,reads=marshal.loads(gzip.decompress((ROOT/"maps/consumer-reads.gz").read_bytes()))
    return {name:1<<i for i,name in enumerate(names)}, set(slots), reads

@lru_cache(1)
def indexes():
    file=ROOT/"cache/consumer-sites"
    return marshal.loads(file.read_bytes()) if file.is_file() else {}

@lru_cache(48)
def sites(selector,path):
    data=indexes()
    if data:return data.get(path,{})
    rows={}
    for n,*spec in scan(path,selector.source_tree(path)):
        rows.setdefault(n,[]).append(spec)
    return rows

@lru_cache(1)
def lexer_guards(selector):
    cache=ROOT/"cache/lexer-guards"
    if cache.is_file():return marshal.loads(cache.read_bytes())
    from consumer_sites import literal
    tokens=selector.source_tree("sqlglot/tokenizer_core.py")
    enums=set()
    for cls in tokens.body:
        if isinstance(cls,ast.ClassDef) and cls.name=="TokenType":
            for stmt in cls.body:
                if isinstance(stmt,ast.Assign):
                    enums.update(t.id for t in stmt.targets if isinstance(t,ast.Name))
    reserved=set()
    for path in selector.SRC:
        if not path.endswith(".py") or not path.startswith("sqlglot/"):continue
        src=selector.SRC[path]
        if "KEYWORDS.pop(" not in src:continue
        for obj in ast.walk(selector.source_tree(path)):
            if isinstance(obj,ast.Call) and isinstance(obj.func,ast.Attribute) and obj.func.attr=="pop" and isinstance(obj.func.value,ast.Name) and obj.func.value.id=="KEYWORDS" and obj.args and literal(obj.args[0]):
                reserved.add(obj.args[0].value)
    return enums,reserved

def refine(selector,text,selected):
    changes=selector.parse_patch(text)
    if len(changes)!=1:return selected
    patch=changes[0];path=patch["old"]
    if not path or path!=patch["new"] or not path.startswith("sqlglot/") or path not in selector.SRC:return selected
    if path in selector.profile()["source_reads"]:return selected
    old=selector.SRC[path]
    new,removed,added=selector.apply(old,patch["hunks"])
    if len(removed)!=1 or len(added)!=1:return selected
    line=removed[0]
    a=old.splitlines();b=new.splitlines()
    if a[:line-1]!=b[:line-1] or a[line:]!=b[line:]:return selected
    left,right=a[line-1],b[line-1]
    if not left.isascii() or not right.isascii():return selected
    for start,end,kind,site,typ,first in sites(selector,path).get(line,()):
        prefix,suffix=left[:start],left[end:]
        if not right.startswith(prefix) or not right.endswith(suffix):continue
        try:value=ast.parse(right[start:len(right)-len(suffix)],mode="eval").body
        except (SyntaxError,ValueError):continue
        bits,slots,reads=observations()
        if set(bits)!=set(selector.NUM):return selected
        if kind in ("key","token"):
            enums,reserved=lexer_guards(selector)
            keys={site}
            if kind=="key":
                if not isinstance(value,ast.Constant) or type(value.value) is not str or not WORD.fullmatch(value.value) or len(value.value)>100:continue
                keys.add(value.value)
            elif not (isinstance(value,ast.Attribute) and isinstance(value.value,ast.Name) and value.value.id=="TokenType" and value.attr in enums):continue
            if keys & reserved:continue
            may=0
            for key in keys:may|=reads.get("key:"+key,0)
        else:
            if not isinstance(value,ast.Constant) or type(value.value).__name__!=typ or site not in slots:continue
            if kind=="default":
                from default_inputs import observations as defaults
                if path+":"+str(first) in defaults()[1]:continue
            may=reads.get(site,0)
        protected=selector.tests_of(selector.M["opens"].get(path))|preserve_scope(selector,path,1<<line,selected)
        protected.update(n for n,(seen,_) in selector.EXTRA.items() if path in seen)
        answer={n for n in selected if n in protected or may & bits[selector.TEST_FILE_ID[n]]}
        if kind in ("key","token"):
            # An inherited tokenizer can read the table without ever executing
            # a line in its declaring dialect during the test itself.
            import isolated_imports
            cold=isolated_imports.readers(selector,path)
            answer.update(n for n in cold if may & bits[selector.TEST_FILE_ID[n]])
        return answer
    return selected
