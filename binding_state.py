"""Stable representations of the Python bindings used by a concrete SQL workload."""

import builtins
import dis
import enum
import functools
import gzip
import hashlib
import logging
import marshal
import re
import sys
import types
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MISSING = object()


class Unknown(ValueError):
    pass


def digest(parts):
    h = hashlib.blake2b(digest_size=20)
    for value in parts:
        h.update(len(value).to_bytes(4, "little"))
        h.update(value)
    return h.digest()


def ident(obj):
    return (obj.__module__ + "." + obj.__qualname__).encode()


def code_hash(code):
    constants = []
    for value in code.co_consts:
        if type(value) is types.CodeType:
            constants.append(code_hash(value))
        elif isinstance(value, frozenset):
            constants.append(digest(sorted(repr(item).encode() for item in value)))
        else:
            constants.append(repr(value).encode())
    structural = (code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount, code.co_flags,
                  code.co_varnames, code.co_names, code.co_cellvars, code.co_freevars)
    return digest([code.co_code, code.co_exceptiontable, repr(structural).encode(), *constants])


class Fingerprints:
    def __init__(self):
        self.memo = {}
        self.active = set()

    def hash(self, value):
        if value is MISSING:
            return b"missing"
        kind = type(value)
        if kind in (str, int, bool, float, bytes, complex, type(None)) or value is Ellipsis or value is NotImplemented:
            return digest([kind.__name__.encode(), repr(value).encode()])
        key = id(value)
        if key in self.memo:
            return self.memo[key][1]
        if key in self.active:
            raise Unknown("cyclic instance")
        self.active.add(key)
        try:
            result = self.expand(value, kind)
        finally:
            self.active.remove(key)
        self.memo[key] = value, result
        return result

    def expand(self, value, kind):
        if isinstance(value, enum.Enum):
            return digest([b"enum", ident(kind), value.name.encode(), self.hash(value.value)])
        if isinstance(value, type):
            return digest([b"type", ident(value), *(ident(c) for c in type.__getattribute__(value, "__mro__"))])
        if kind is types.ModuleType:
            return digest([b"module", value.__name__.encode()])
        if kind is types.FunctionType:
            closure = [self.hash(c.cell_contents) for c in value.__closure__ or ()]
            return digest([b"function", ident(value), code_hash(value.__code__), self.hash(value.__defaults__),
                           self.hash(value.__kwdefaults__), *closure])
        if kind in (staticmethod, classmethod):
            return digest([kind.__name__.encode(), self.hash(value.__func__)])
        if kind is property:
            return digest([b"property", self.hash(value.fget), self.hash(value.fset), self.hash(value.fdel)])
        if kind in (dict, defaultdict, types.MappingProxyType):
            # Iteration order is visible to production code.
            pairs = [digest([self.hash(k), self.hash(v)]) for k, v in value.items()]
            factory = self.hash(value.default_factory) if kind is defaultdict else b""
            return digest([kind.__name__.encode(), factory, *pairs])
        if kind in (tuple, list, set, frozenset):
            items = [self.hash(v) for v in value]
            if kind in (set, frozenset):
                items.sort()
            return digest([kind.__name__.encode(), *items])
        if kind is types.MethodType:
            return digest([b"bound", self.hash(value.__func__), self.hash(value.__self__)])
        if isinstance(value, (types.MemberDescriptorType, types.GetSetDescriptorType,
                              types.WrapperDescriptorType, types.MethodDescriptorType)):
            return digest([kind.__name__.encode(), ident(value.__objclass__), value.__name__.encode()])
        if kind in (types.BuiltinMethodType, types.BuiltinFunctionType):
            owner = value.__self__
            if owner is not None and not isinstance(owner, (type, types.ModuleType)):
                raise Unknown("external instance method")
            return digest([b"builtin", (value.__qualname__).encode(), self.hash(owner)])
        if isinstance(value, logging.Logger):
            return digest([b"logger", value.name.encode(), str(value.level).encode()])
        if isinstance(value, re.Pattern):
            return digest([b"regex", self.hash(value.pattern), self.hash(value.flags)])
        if isinstance(value, functools.partial):
            return digest([b"partial", self.hash(value.func), self.hash(value.args), self.hash(value.keywords)])
        if kind.__module__.startswith("sqlglot"):
            pieces = [ident(kind)]
            fields = getattr(value, "__dict__", None)
            if fields is not None:
                pieces.append(self.hash(fields))
            for base in type.__getattribute__(kind, "__mro__"):
                namespace = type.__getattribute__(base, "__dict__")
                slots = namespace.get("__slots__", ())
                for key in ((slots,) if isinstance(slots, str) else slots):
                    if key not in ("__dict__", "__weakref__"):
                        pieces.extend([key.encode(), self.hash(getattr(value, key, MISSING))])
            return digest(pieces)
        raise Unknown(kind.__module__ + "." + kind.__qualname__)


def resolve(owner):
    module, cls, name = owner
    mod = sys.modules.get(module)
    if mod is None:
        raise Unknown("module is not loaded " + module)
    if cls == "":
        return vars(mod).get(name, vars(builtins).get(name, MISSING))
    ob = mod
    for attr in cls.split("."):
        ob = vars(ob).get(attr, MISSING)
        if ob is MISSING:
            raise Unknown("unavailable class " + module + "." + cls)
    if not isinstance(ob, type):
        raise Unknown("receiver ceased to be a class")
    for base in (*type.__getattribute__(ob, "__mro__"), *type.__getattribute__(type(ob), "__mro__")):
        namespace = type.__getattribute__(base, "__dict__")
        if name in namespace:
            return namespace[name]
    return MISSING


def profile():
    return marshal.loads(gzip.decompress((ROOT/"maps/node-state.gz").read_bytes()))


def live(keys):
    finger = Fingerprints()
    results, unknown = {}, {}
    for key in keys:
        try:
            results[key] = finger.hash(resolve(key))
        except (Unknown, TypeError, AttributeError, RecursionError, ValueError) as exc:
            unknown[key] = str(exc)
    return results, unknown
