"""Trace actual successful and unsuccessful reads from each concrete tokenizer's configuration."""

import atexit
import gzip
import importlib.abc
import importlib.machinery
import marshal
import os
from pathlib import Path
import sys

active = os.environ.get("BR_FLAG_TEST","")
loading = 0
installed = False
reads = {}
cache = {}

FIELDS = ("single_tokens","keywords","quotes","format_strings","identifiers","comments",
          "string_escapes","byte_string_escapes","identifier_escapes","commands","command_prefix_tokens",
          "tokens_preceding_hint","numeric_literals","var_single_tokens","keyword_trie",
          "unescaped_sequences","numeric_escapes")


def keypart(value):
    if type(value) in (str,int):
        return value
    if type(value).__name__ == "TokenType" and isinstance(value,int):
        return ("token",value.name)
    return ("unsupported",)


class Reader:
    __slots__ = ("source","site","field","prefix","children","last","bucket")

    def __init__(self,source,site,field,prefix=()):
        self.source=source
        self.site=site
        self.field=field
        self.prefix=prefix
        self.children={}
        self.last=None
        self.bucket=None

    def note(self,op,key=None):
        context="" if loading else active
        if self.last!=context:
            self.last=context
            self.bucket=reads.setdefault((self.site,self.field,context),set())
        if op in ("all","truth"):
            self.bucket.add((self.prefix,op))
        else:
            k=keypart(key)
            self.bucket.add((self.prefix+(k,),op) if k!=("unsupported",) else (self.prefix,"all"))

    def __contains__(self,key):
        self.note("has",key)
        return key in self.source

    def get(self,key,default=None):
        value=self.source.get(key,default)
        if self.field=="keyword_trie":
            self.note("kind",key)
            if type(value) is dict:
                if key not in self.children:
                    self.children[key]=Reader(value,self.site,self.field,self.prefix+(keypart(key),))
                return self.children[key]
        else:
            self.note("get",key)
        return value

    def __getitem__(self,key):
        self.note("get",key)
        return self.source[key]

    def __bool__(self):
        self.note("truth")
        return bool(self.source)

    def __iter__(self):
        self.note("all")
        return iter(self.source)

    def __len__(self):
        self.note("all")
        return len(self.source)

    def __or__(self,value):
        self.note("all")
        return self.source | value

    def items(self):
        self.note("all")
        return self.source.items()

    def keys(self):
        self.note("all")
        return self.source.keys()

    def values(self):
        self.note("all")
        return self.source.values()

    def copy(self):
        self.note("all")
        return self.source.copy()


def configure(module):
    global installed
    original=module.Tokenizer._init_core
    def capture(self):
        core=original(self)
        if len(cache)>1800:
            cache.clear()
        site=type(self).__module__+":"+type(self).__qualname__
        for field in FIELDS:
            value=getattr(core,field)
            key=site,field,id(value)
            if key not in cache:
                cache[key]=Reader(value,site,field)
            setattr(core,field,cache[key])
        return core
    module.Tokenizer._init_core=capture
    installed=True


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self,module):
        global loading
        loading+=1
        try:
            super().exec_module(module)
            if module.__name__=="sqlglot.tokens":
                configure(module)
        finally:
            loading-=1


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname=="sqlglot" or fullname.startswith("sqlglot."):
            spec=importlib.machinery.PathFinder.find_spec(fullname,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader=Loader(fullname,spec.origin)
                return spec


def save():
    p=Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".reads")
    p.write_bytes(gzip.compress(marshal.dumps((installed,reads),2),mtime=0))


if "BR_FLAG_DIR" in os.environ:
    sys.meta_path.insert(0,Finder())
    atexit.register(save)
