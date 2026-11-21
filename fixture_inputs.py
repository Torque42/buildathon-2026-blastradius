"""Compare fixture loader outputs and the fields consumed by each pristine test."""
import gzip
import json
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

ROOT=Path(__file__).resolve().parent

@lru_cache(maxsize=1)
def profiles():
    def read_map(name):return json.loads(gzip.decompress((ROOT/"maps"/name).read_bytes()))
    return read_map("fixture-readers.json.gz"),read_map("fixture-fields.json.gz")


def lines(text):
    return "\n".join(line for line in text.splitlines() if line and not line.startswith("--"))


def pairs(text):
    statements=lines(text).split(";")
    output=[]
    for i in range(0,len(statements)-1,2):
        sql=statements[i].strip().split("\n")
        meta={};n=0
        while sql[n].startswith("#"):
            key,val=sql[n].split(":",1)
            meta[key.lstrip("#").strip()]=val.strip()
            n+=1
        output.append((meta,"\n".join(sql[n:]),statements[i+1].strip()))
    return output


def line_cases(text):
    cases=[];buffer=[]
    for line in lines(text).splitlines():
        buffer.append(line)
        if not line.startswith("#"):
            cases.append("\n".join(buffer));buffer=[]
    return cases


def expand(selector,ids):
    out=set()
    for node in ids:
        if node.startswith("scope:") and node[6:] in selector.BY_FILE:out.update(selector.BY_FILE[node[6:]])
        elif node in selector.NUM:out.add(selector.NUM[node])
        else:return set(selector.ALL)
    return out


def same_executor_queries(name,before,after,changes,accesses):
    from query_replay import IDS,profile
    kind=next((kind for kind,node in IDS.items() if node==name),None)
    if kind is None:return False
    data=profile()[2][kind]
    if len(data)!=len(before):return False
    pending=[]
    for i,mask in changes.items():
        if not accesses.get(str(i),0)&mask:continue
        old_meta,old_sql,_=before[i]
        new_meta,new_sql,_=after[i]
        if old_meta!=new_meta or data[i]["sql"]!=old_sql or data[i]["execute"]!=bool(old_meta.get("execute")):
            return False
        if old_sql!=new_sql:pending.append((old_sql,new_sql,data[i]["execute"]))
    if not pending:return True
    if len(pending)>4:return False
    try:
        result=subprocess.run([sys.executable,"-I",str(ROOT/"fixture_parse.py")],input=json.dumps(pending).encode(),
                              capture_output=True,timeout=1.2)
    except (OSError,subprocess.TimeoutExpired):return False
    return result.returncode==0 and result.stdout.strip()==b"matched"


def refine_one(selector,patch,original):
    path=patch["old"]
    data,fields=profiles()
    old=data["contents"].get(path)
    if path!=patch["new"] or old is None or set(selector.NUM)!=set(data["catalog"]):return original
    # Apply only patches whose old lines agree with the actual pinned fixture.
    saved=old.splitlines();pos=0
    for h in patch["hunks"]:
        pos=h["os"]-1
        for raw in h["lines"]:
            if raw[:1] in (" ","-"):
                if pos<0 or pos>=len(saved) or saved[pos]!=raw[1:].rstrip("\r"):return original
                pos+=1
    new,_,_=selector.apply(old,patch["hunks"])
    modes=data["readers"][path]
    keep=set()
    dangerous=False
    raw_readers=set()
    before=after=None
    for mode,ids in modes.items():
        if mode=="load_sql_fixture_pairs":
            try:before=pairs(old);after=pairs(new)
            except (ValueError,IndexError):
                dangerous=True;keep.update(expand(selector,ids));continue
            if before==after:continue
        elif mode=="load_sql_fixtures" and line_cases(old)==line_cases(new):continue
        retained=expand(selector,ids)
        keep.update(retained)
        if mode!="load_sql_fixture_pairs":raw_readers.update(retained)
    if dangerous:
        # A failed class fixture can prevent every test in the scope from starting.
        return original|keep
    # Other known readers of this file may be attributed by the baseline
    # through suite initialization or fixture scoping.
    consumers=expand(selector,set().union(*(set(ids) for ids in modes.values())))
    result=(original-consumers)|keep
    if path in fields["paths"] and before is not None and after is not None and len(before)==len(after):
        changes={i:sum(1<<j for j,(a,b) in enumerate(zip(x,y)) if a!=b)
                 for i,(x,y) in enumerate(zip(before,after)) if x!=y}
        for name,by_file in fields["reads"].items():
            if name not in selector.NUM or selector.NUM[name] in raw_readers:continue
            accesses=by_file.get(path,{})
            if all(not (accesses.get(str(i),0)&mask) for i,mask in changes.items()) or same_executor_queries(name,before,after,changes,accesses):
                result.discard(selector.NUM[name])
    return result | (original & selector.UNTRACED_POOL_TESTS)


def refine(selector,text,selected):
    patches=selector.parse_patch(text)
    if not patches or any(not p["old"] or not p["old"].startswith("tests/fixtures/") or not p["old"].endswith(".sql") for p in patches):
        return selected
    if len(patches)==1:return refine_one(selector,patches[0],selected)
    return selected
