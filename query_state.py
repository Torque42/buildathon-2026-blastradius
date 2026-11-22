"""Resolve the precise class table entries used by a query, including inherited and derived tables."""

import enum
import importlib
import sys

import binding_state


def load_modules(names):
    import unittest
    for name in names:
        if name not in sys.modules:
            try:
                importlib.import_module(name)
            except ImportError:
                pass


def key_value(key):
    if key[0] == "literal":
        return key[1]
    _, module, name, *args = key
    obj = sys.modules.get(module)
    if obj is None:
        raise binding_state.Unknown("unloaded key type")
    for attr in name.split("."):
        obj=vars(obj).get(attr)
        if obj is None:
            raise binding_state.Unknown("missing key type")
    if key[0] == "type" and isinstance(obj,type):
        return obj
    if key[0] == "enum" and isinstance(obj,type) and issubclass(obj,enum.Enum):
        return obj.__members__[args[0]]
    raise binding_state.Unknown("key changed type")


def projected(desc, finger, resolved, keys):
    scope=desc[:3]
    if scope not in resolved:
        resolved[scope]=binding_state.resolve(scope)
    value=resolved[scope]
    mode, symbol=desc[3:]
    if not mode:
        # On the pin these tables are spread into other lookup tables or
        # consumed by TypeAnnotator.get, membership and keyed subscripting.
        # Their construction iterates sets of class objects. Class allocation
        # addresses can change their insertion order between two imports.
        if (scope[2] in {"EXPRESSION_METADATA", "BINARY_COERCIONS"}
                and type(value) is dict):
            material = sorted(binding_state.digest([finger.hash(k), finger.hash(v)])
                              for k,v in value.items())
            return binding_state.digest([b"lookup contents", *material])
        if scope == ("sqlglot.optimizer.canonicalize", "", "_CANONICALIZE_TYPES"):
            # Its sole consumer is isinstance(node, tuple_of_types).
            if type(value) is tuple and all(isinstance(cls,type)
                    and type(cls).__instancecheck__ is type.__instancecheck__ for cls in value):
                return binding_state.digest([b"isinstance types", *sorted(finger.hash(cls) for cls in value)])
        return finger.hash(value)
    if symbol not in keys:
        keys[symbol]=key_value(symbol)
    key=keys[symbol]
    if mode=="lookup" and type(value) is dict:
        return binding_state.digest([b"lookup",finger.hash(value.get(key,binding_state.MISSING))])
    if mode=="member" and type(value) in (dict, set, frozenset):
        return binding_state.digest([b"member",type(value).__name__.encode(),finger.hash(key in value)])
    raise binding_state.Unknown("container changed behavior")
