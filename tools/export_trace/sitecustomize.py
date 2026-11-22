"""Observe reads of exported SQL expression classes during the pristine test suite."""
import atexit
import functools
import gzip
import importlib.abc
import importlib.machinery
import marshal
import os
import sys
import types
from pathlib import Path

active = os.environ.get("BR_FLAG_TEST","")
events = {}
eligible = set()
imports = []
track = False


def record(name, frame):
    if imports:
        owner = imports[-1]
        # Package initialization builds the expression registry and re-exports.
        # Its invariants are checked on the pinned source by the consumer.
        if owner=="sqlglot.expressions":
            return
        filename = frame.f_code.co_filename
        if "/sqlglot/" in filename:
            location = "sqlglot/"+filename.split("/sqlglot/",1)[1]
            if location=="sqlglot/helper.py" and frame.f_code.co_name=="subclasses":
                location=owner.replace(".","/")+".py"
            key=("init",location,frame.f_lineno,owner)
        else:
            key=("init",owner.replace(".","/")+".py",0,owner)
        ctx = "startup"
    else:
        key=("read",)
        ctx=active
        check=frame
        while not ctx and check:
            filename=check.f_code.co_filename.replace("\\","/")
            if "/tests/" in filename and "/site-packages/" not in filename:
                ctx="scope:tests/"+filename.split("/tests/",1)[1]
            check=check.f_back
        ctx=ctx or "startup"
    events.setdefault(name,{}).setdefault(ctx,set()).add(key)


class Namespace(types.ModuleType):
    def __getattribute__(self, key):
        if track and key in eligible:
            frame=sys._getframe(1)
            observed=True
            if frame.f_globals.get("__name__")=="inspect":
                parent=frame
                while parent and parent.f_globals.get("__name__")=="inspect":
                    parent=parent.f_back
                if (parent and parent.f_globals.get("__name__")=="sqlglot.helper"
                        and parent.f_code.co_name=="subclasses"):
                    value=types.ModuleType.__getattribute__(self,key)
                    observed=isinstance(value,type) and issubclass(value,parent.f_locals["classes"])
            if observed:record(key,frame)
        if track and key=="__dict__" and not imports:
            frame=sys._getframe(1)
            if not frame.f_globals.get("__name__","").startswith(("importlib.","_frozen_importlib")):
                record("*",frame)
        return types.ModuleType.__getattribute__(self,key)

    def __dir__(self):
        if track and not imports:
            record("*",sys._getframe(1))
        return types.ModuleType.__dir__(self)


class Registry(dict):
    def get(self,key,*args):
        if type(key) is str and key in lookup:record(lookup[key],sys._getframe(1))
        return super().get(key,*args)

    def __getitem__(self,key):
        if type(key) is str and key in lookup:record(lookup[key],sys._getframe(1))
        return super().__getitem__(key)

    def __contains__(self,key):
        if type(key) is str and key in lookup:record(lookup[key],sys._getframe(1))
        return super().__contains__(key)

    def __iter__(self):
        record("*",sys._getframe(1))
        return super().__iter__()

    def keys(self):
        record("*",sys._getframe(1))
        return super().keys()

    def items(self):
        record("*",sys._getframe(1))
        return super().items()

    def values(self):
        record("*",sys._getframe(1))
        return super().values()

    def __len__(self):
        record("*",sys._getframe(1))
        return super().__len__()


def install(module):
    global lookup, track
    ns=vars(module)
    for name,cls in ns.items():
        if isinstance(cls,type) and cls.__module__.startswith("sqlglot.expressions.") and cls.__name__==name:
            eligible.add(name)
    lookup={name.lower():name for name in eligible}
    ns["EXPR_CLASSES"]=Registry(ns["EXPR_CLASSES"])
    for name,child in tuple(sys.modules.items()):
        if name=="sqlglot.expressions" or name.startswith("sqlglot.expressions."):
            if type(child) is types.ModuleType:
                child.__class__=Namespace
    def wrap(original):
        @functools.wraps(original)
        def init(instance,*args,**kwargs):
            name=type(instance).__name__
            if name in eligible:
                record(name,sys._getframe(1))
            original(instance,*args,**kwargs)
        return init
    for cls in (ns["Expr"],ns["Expression"]):
        if "__init__" in vars(cls):
            cls.__init__=wrap(vars(cls)["__init__"])
    track=True


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self,module):
        imports.append(module.__name__)
        try:
            super().exec_module(module)
            if module.__name__=="sqlglot.expressions":
                install(module)
        finally:
            imports.pop()


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self,name,path=None,target=None):
        if name=="sqlglot" or name.startswith("sqlglot."):
            spec=importlib.machinery.PathFinder.find_spec(name,path)
            if spec and isinstance(spec.loader,importlib.machinery.SourceFileLoader):
                spec.loader=Loader(name,spec.origin)
            return spec


def save():
    if track:
        events.setdefault("@installed",{})["startup"]={True}
        (Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".rec")).write_bytes(
            gzip.compress(marshal.dumps(events),3,mtime=0))

if "BR_FLAG_DIR" in os.environ:
    sys.meta_path.insert(0,Finder())
    atexit.register(save)
