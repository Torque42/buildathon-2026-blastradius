"""Observe constructor arguments and parser slot reads in pristine pytest processes."""
import atexit
import functools
import importlib.abc
import importlib.machinery
import inspect
import json
import os
import sys
import types
from pathlib import Path

OWNERS = {"sqlglot.parser": "Parser", "sqlglot.planner": "SetOperation"}
FIELDS = {"Parser": ("max_errors", "max_nodes", "error_message_context"),
          "SetOperation": ("distinct",)}
installed = set()
omitted = set()
read = set()
uncertain = set()
calls = {}


class ObservedSlot:
    def __init__(self, original, key):
        self.original = original
        self.key = key

    def __get__(self, instance, cls=None):
        read.add(self.key)
        return self.original.__get__(instance, cls)

    def __set__(self, instance, value):
        self.original.__set__(instance, value)

    def __delete__(self, instance):
        self.original.__delete__(instance)


def instrument(cls):
    owner = cls.__name__
    original = cls.__init__
    positions = list(inspect.signature(original).parameters)
    slots = {}
    for field in FIELDS[owner]:
        key = f"{owner}.{field}"
        descriptor = vars(cls).get(field)
        if owner == "Parser":
            if not isinstance(descriptor, types.MemberDescriptorType):
                raise TypeError(key)
            slots[field] = ObservedSlot(descriptor, key)
            setattr(cls, field, slots[field])

    @functools.wraps(original)
    def traced(instance, *args, **kwargs):
        calls[owner] = calls.get(owner, 0) + 1
        for field in FIELDS[owner]:
            key = f"{owner}.{field}"
            if positions.index(field) > len(args) and field not in kwargs:
                omitted.add(key)
                if field in slots:
                    typ = type(instance)
                    slot = next(vars(base)[field] for base in typ.__mro__ if field in vars(base))
                    if slot is not slots[field] or typ.__setattr__ is not object.__setattr__:
                        uncertain.add(key)
        return original(instance, *args, **kwargs)

    cls.__init__ = traced
    installed.add(owner)


class Loader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module):
        owner = OWNERS[module.__name__]
        # Detect calls made before the class is available for instrumentation.
        previous = sys.getprofile()
        def watch(frame, event, arg):
            if event == "call" and frame.f_code.co_qualname == owner + ".__init__":
                uncertain.update(f"{owner}.{field}" for field in FIELDS[owner])
            if previous:
                previous(frame, event, arg)
        sys.setprofile(watch)
        try:
            super().exec_module(module)
        finally:
            sys.setprofile(previous)
        instrument(getattr(module, owner))


class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname in OWNERS:
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
                return spec
            uncertain.update(f"{OWNERS[fullname]}.{field}" for field in FIELDS[OWNERS[fullname]])


def save():
    record = dict(installed=sorted(installed), calls=calls, read=sorted(read),
                  omitted=sorted(omitted), uncertain=sorted(uncertain))
    (Path(os.environ["BR_CONSTRUCTOR_LOG"]) / f"{os.getpid()}.json").write_text(json.dumps(record))


if "BR_CONSTRUCTOR_LOG" in os.environ:
    sys.meta_path.insert(0, Finder())
    atexit.register(save)
