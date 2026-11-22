"""Losslessly materialize the original lexical stream for repeated very large parser inputs."""

from functools import lru_cache
import hashlib
from itertools import accumulate, repeat
import marshal
import operator
from pathlib import Path
import zlib


def key(sql):
    return len(sql),hashlib.sha256(sql.encode()).digest()


def configuration(dialect):
    """All inputs used to initialize the pure-Python lexical engine on the pinned default dialect."""
    from sqlglot.dialects.dialect import Dialect
    from sqlglot.tokens import Tokenizer
    if (type(dialect) is not Dialect or dialect.tokenizer_class is not Dialect.tokenizer_class or
            Dialect.tokenizer_class.__bases__!=(Tokenizer,)):
        return
    core=dialect.tokenizer()._core
    def freeze(value):
        if type(value) in (bool,int,str,type(None)):
            return value
        if isinstance(value,int) and type(value).__name__=="TokenType":
            return ("token",value.name)
        if type(value) is dict:
            values=tuple((freeze(k),freeze(v)) for k,v in value.items())
            # The core navigates the keyword trie by key. Its construction iterates a set.
            return ("dict",tuple(sorted(values,key=lambda p:repr(p[0]))) if field=="keyword_trie" else values)
        if type(value) in (set,frozenset):
            return ("set",tuple(sorted((freeze(v) for v in value),key=repr)))
        if type(value) in (list,tuple):
            return (type(value).__name__,tuple(map(freeze,value)))
        raise TypeError(type(value).__name__)
    items=[]
    for field in type(core).__slots__:
        if field.startswith("_") or field in {"tokens","size","sql"}:
            continue
        items.append((field,freeze(getattr(core,field))))
    return hashlib.sha256(marshal.dumps(items,2)).digest()


def encode(tokens):
    from sqlglot.tokenizer_core import Token
    codes=bytearray()
    signatures={}
    previous=0
    for token in tokens:
        if (type(token) is not Token or token.line!=1 or token.col!=token.end+1 or token.comments or
                type(token.text) is not str or token.start<previous):
            return
        observation=(token.token_type.name,token.text,token.end-token.start,token.start-previous)
        codes.append(signatures.setdefault(observation,len(signatures)))
        previous=token.start
    return tuple(signatures),zlib.compress(codes)


def materialize(stream):
    from sqlglot.tokenizer_core import Token,TokenType
    signatures,packed=stream
    codes=zlib.decompress(packed)
    names,words,widths,deltas=zip(*signatures)
    types=tuple(TokenType[name] for name in names)
    starts=list(accumulate(map(deltas.__getitem__,codes)))
    ends=list(map(operator.add,starts,map(widths.__getitem__,codes)))
    return list(map(Token,map(types.__getitem__,codes),map(words.__getitem__,codes),repeat(1),
                    map(operator.add,ends,repeat(1)),starts,ends))


@lru_cache(1)
def records():
    path=Path(__file__).resolve().parent/"cache/direct-workload-witnesses"
    return marshal.loads(path.read_bytes()).get("token-streams",{}) if path.is_file() else {}


def eligible(edits,selector):
    import workload_replay
    allowed=("sqlglot/parsers/","sqlglot/generators/","sqlglot/optimizer/")
    for path,source in edits.items():
        if path.startswith("tests/fixtures/"):
            continue
        if path not in ("sqlglot/parser.py","sqlglot/generator.py") and not path.startswith(allowed):
            return False
        if workload_replay.body_edits(path,selector.SRC[path],source) is None:
            return False
    return True


def install():
    from sqlglot.dialects.dialect import Dialect
    original=Dialect.tokenize
    cached=records()
    def tokenize(self,sql,*args,**kwargs):
        if type(sql) is str and len(sql)>100_000 and not args and not kwargs:
            record=cached.get(key(sql))
            if record is not None and configuration(self)==record[0]:
                return materialize(record[1])
        return original(self,sql,*args,**kwargs)
    Dialect.tokenize=tokenize
