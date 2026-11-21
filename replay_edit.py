import gzip,json
from pathlib import Path
from functools import lru_cache
import importlib.abc
import importlib.util
@lru_cache(4)
def possible_edit(selector,text):
    files=selector.parse_patch(text)
    if len(files)!=1:
        return
    file=files[0]
    path=file["old"]
    if path!=file["new"] or not path or not path.startswith("sqlglot/") or path not in selector.SRC:
        return
    original=selector.SRC[path]
    new,removed,inserted=selector.apply(original,file["hunks"])
    if len(removed)!=1 or len(inserted)!=1:
        return
    line=removed[0]
    before=original.splitlines();after=new.splitlines()
    if before[:line-1]!=after[:line-1] or before[line:]!=after[line:]:
        return
    # Only isolated operand and literal edits in an existing function.
    # Moved code, added calls, changed function signatures, and definitions
    # can have effects before the first traced operation.
    span=selector.FACTS.get("statements",{}).get(path,{}).get(line)
    if not span or not selector.simple_token_change(before[line-1],after[line-1]):
        return
    if path in selector.profile()["source_reads"]:
        return
    start,end,_=span
    return path,new,((1<<(end-start+1))-1)<<start


class Overlay(importlib.abc.MetaPathFinder,importlib.abc.Loader):
    def __init__(self,path,source,root):
        self.name=path[:-3].replace("/",".").removesuffix(".__init__")
        self.source=source
        self.file=root/path

    def find_spec(self,fullname,path=None,target=None):
        if fullname==self.name:
            return importlib.util.spec_from_file_location(fullname,self.file,loader=self)

    def create_module(self,spec):
        return None

    def exec_module(self,module):
        exec(compile(self.source,str(self.file),"exec",dont_inherit=True),module.__dict__)



@lru_cache(1)
def inspected():
    return json.loads(gzip.decompress((Path(__file__).parent/"maps/inspection.gz").read_bytes()))

def preserve(selector,text,selected):
    edit=possible_edit(selector,text)
    if not edit:return set()
    path,_,mask=edit
    return preserve_scope(selector,path,mask,selected)


def preserve_scope(selector,path,mask,selected):
    if not any((1<<line)&mask for line in selector.profile()["code_reads"].get(path,())):return set()
    profile=inspected()
    if set(profile["catalog"])!=set(selector.NUM):return selected
    retained=set()
    for (filename,start,end),ids in profile["reads"]:
        if filename==path and (((1<<(end-start+1))-1)<<start)&mask:
            if any(n not in selector.NUM for n in ids):return selected
            retained.update(selector.NUM[n] for n in ids)
    return selected&retained
