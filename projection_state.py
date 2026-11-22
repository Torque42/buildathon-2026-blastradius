"""Match the complete initialized parser state at its long projection call."""
from enum import Enum
import hashlib
import marshal


def token(value):
    from sqlglot.tokenizer_core import Token
    if type(value) is not Token:
        raise ValueError("Unrecognized token")
    return value.token_type.name,value.text,value.line,value.col,value.start,value.end,tuple(value.comments or ())


def state(parser,include_tokens):
    from sqlglot.dialects.dialect import Dialect
    import token_streams
    slots=set()
    for cls in type(parser).__mro__:
        slots.update(vars(cls).get("__slots__",()))
    tokens=parser._tokens
    if (len(parser._chunks)!=1 or parser._chunks[0] is not tokens or parser._tokens_size!=len(tokens)
        or len(tokens)<100_000 or type(tokens) is not list or type(parser.dialect) is not Dialect or parser.errors):
        raise ValueError("Unrecognized parser instance")
    fixed={"_tokens","_chunks","sql","dialect","_curr","_next","_prev","_prev_comments","errors"}
    scalars={}
    for n in slots-fixed-{"__dict__","__weakref__"}:
        v=getattr(parser,n)
        if isinstance(v,Enum):
            v=type(v).__module__,type(v).__qualname__,v.name
        elif type(v) not in (int,bool,str,type(None)):
            raise ValueError(n)
        scalars[n]=v
    if getattr(parser,"__dict__",{}):
        raise ValueError("Extended parser state")
    idx=parser._index
    from sqlglot.parser import SENTINEL_NONE
    if any(getattr(parser,k) is not (tokens[idx+i] if 0<=idx+i<len(tokens) else SENTINEL_NONE)
           for k,i in (("_curr",0),("_prev",-1),("_next",1))):
        raise ValueError("Unexpected token cursor")
    if idx>0 and parser._prev_comments is not parser._prev.comments:
        raise ValueError("Unexpected comment alias")
    import binding_state
    checkpoint=(tuple(sorted(scalars.items())),tuple(token(getattr(parser,k)) for k in ("_prev","_curr","_next")),
                binding_state.Fingerprints().hash(parser.dialect),tuple(parser._prev_comments or ()))
    if include_tokens:
        stream=token_streams.encode(tokens)
        if stream is None:
            raise ValueError("Unrecognized token representation")
        checkpoint=checkpoint,hashlib.sha256(marshal.dumps(stream,2)).digest()
    return checkpoint


def restore(parser,saved):
    scalars=dict(saved[0])
    # All other simple fields must retain their actual entry values. Set only
    # the positions and monotonically advancing node counter recorded at exit.
    parser._index=scalars["_index"]
    parser._node_count=scalars["_node_count"]
    i=parser._index
    from sqlglot.parser import SENTINEL_NONE
    parser._prev,parser._curr,parser._next=(parser._tokens[j] if 0<=j<len(parser._tokens) else SENTINEL_NONE for j in (i-1,i,i+1))
    parser._prev_comments=parser._prev.comments
    if state(parser,False)!=saved:
        raise ValueError("Projection exit state differs")
