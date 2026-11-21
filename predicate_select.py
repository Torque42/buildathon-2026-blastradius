"""Select tests whose observed primitive evaluations could change."""
import ast
import gzip
import marshal
from functools import lru_cache
from pathlib import Path
from predicates import location,OPS
from replay_edit import possible_edit


@lru_cache(1)
def data():
    root=Path(__file__).parent
    cached=root/"cache/predicate-observations"
    return marshal.loads(cached.read_bytes() if cached.is_file() else gzip.decompress((root/"maps/predicate-observations.gz").read_bytes()))


def changes(a,b):
    if isinstance(a,ast.AST) and type(a) is type(b):
        if isinstance(a,ast.Compare) and len(a.ops)==len(b.ops)==1:
            if type(a.ops[0]) is not type(b.ops[0]) and equal(a.left,b.left) and equal(a.comparators,b.comparators):
                yield a,"operator",(type(a.ops[0]).__name__,type(b.ops[0]).__name__)
                return
            if type(a.ops[0]) is type(b.ops[0]):
                for pos,(old,new) in enumerate(zip((a.left,a.comparators[0]),(b.left,b.comparators[0]))):
                    if isinstance(old,ast.Constant) and isinstance(new,ast.Constant) and type(old.value)==type(new.value) and old.value!=new.value and equal((a.comparators[0],a.left)[pos],(b.comparators[0],b.left)[pos]):
                        yield a,"threshold",(type(a.ops[0]).__name__,pos,old.value,new.value)
                        return
        if isinstance(a,ast.BoolOp) and type(a.op)!=type(b.op) and equal(a.values,b.values):
            yield a,"reach","bool";return
        if isinstance(a,ast.Constant) and (type(a.value)!=type(b.value) or a.value!=b.value):
            yield a,"reach","literal";return
        for key,value in ast.iter_fields(a):yield from changes(value,getattr(b,key))
    elif isinstance(a,list) and isinstance(b,list) and len(a)==len(b):
        for old,new in zip(a,b):yield from changes(old,new)
    elif type(a)!=type(b) or a!=b:
        if isinstance(a,ast.UnaryOp) and isinstance(a.op,ast.Not) and equal(a.operand,b):
            yield a,"reach","not"
        elif isinstance(b,ast.UnaryOp) and isinstance(b.op,ast.Not) and equal(a,b.operand):
            yield a,"reach","condition"
        else:yield None,"other",None


def equal(a,b):
    if isinstance(a,list) and isinstance(b,list):
        return len(a)==len(b) and all(equal(x,y) for x,y in zip(a,b))
    return type(a)==type(b) and ast.dump(a)==ast.dump(b) if isinstance(a,ast.AST) and isinstance(b,ast.AST) else a==b


def outcome(op,rel,identity):
    if op=="Is":return identity
    if op=="IsNot":return not identity
    if rel=="X":return None
    if op=="Eq":return rel in ("=","U=")
    if op=="NotEq":return rel not in ("=","U=")
    if rel.startswith("U"):return None
    return rel in {"Lt":"<","LtE":"<=","Gt":">","GtE":">="}[op]


@lru_cache(1)
def numbers(selector):
    catalog,_=data()
    return {node:(1<<i) for i,node in enumerate(catalog)}


def refine(selector,text,selected):
    if not selected:return selected
    edit=possible_edit(selector,text)
    if not edit:return selected
    path,new,mask=edit
    start=(mask&-mask).bit_length()-1;end=mask.bit_length()-1
    def snippet(source):
        lines=source.splitlines()[start-1:end]
        if not lines or not lines[0][:1].isspace():raise SyntaxError
        if lines[-1].rstrip().endswith(":"):
            indent=lines[0][:len(lines[0])-len(lines[0].lstrip())]
            lines.append(indent+"    pass")
        return ast.parse("if True:\n"+"\n".join(lines)).body[0].body
    try:
        diffs=list(changes(snippet(selector.SRC[path]),snippet(new)))
        offset=start-2
    except (SyntaxError,IndexError,ValueError):
        diffs=list(changes(selector.source_tree(path),ast.parse(new)))
        offset=0
    if len(diffs)!=1:return selected
    node,mode,detail=diffs[0]
    if not node:return selected
    typ=detail if mode=="reach" else "cmp"
    _,sites=data()
    record=sites.get(f"{path}:{node.lineno+offset}:{node.col_offset}:{typ}")
    if not record:return selected
    known,reached,relations,values=record
    may=reached
    if mode=="operator" and all(s in OPS for s in detail):
        may=0
        for (rel,identity),bits in relations.items():
            a,b=[outcome(op,rel,identity) for op in detail]
            if a is None or b is None or a!=b:may|=bits
    if mode=="threshold" and detail[0] in OPS and detail[0] not in ("Is","IsNot"):
        op,pos,old,new=detail
        if isinstance(node.left,ast.Constant) and isinstance(node.comparators[0],ast.Constant):return selected
        may=reached
        if values:
            described=0;uncertain=0
            for val,bits in values.items():
                described|=bits
                if val=="*":uncertain|=bits;continue
                value=val[1]
                try:
                    f=OPS[op]
                    a,b=(f(old,value),f(new,value)) if pos==0 else (f(value,old),f(value,new))
                except (ValueError,TypeError,OverflowError):uncertain|=bits;continue
                if a!=b:uncertain|=bits
            may=uncertain|(reached&~described)
    safe=known&~may
    preserved=selector.tests_of(selector.M["opens"].get(path))
    bits=numbers(selector)
    return {n for n in selected if n in preserved or not safe&bits.get(selector.TEST_FILE_ID[n],0)}
