"""Retain tests when the patched conditional can change a decision or an observable operand."""
import ast
import gzip
import marshal
from functools import lru_cache
from pathlib import Path

from branch_model import tree, unchanged
from replay_edit import possible_edit, preserve

@lru_cache(1)
def facts():
    names,sites=marshal.loads(gzip.decompress((Path(__file__).parent/"maps/branch-states.gz").read_bytes()))
    return {n:1<<i for i,n in enumerate(names)},sites

def simple_identity(spec):
    if spec[0]=="name":return True
    if spec[0]=="constant":return spec[1] is None or type(spec[1]) is bool
    if spec[0]=="attr":return simple_identity(spec[1])
    if spec[0]=="get":return simple_identity(spec[1]) and (len(spec[2])==1 or simple_identity(spec[2][1]))
    if spec[0]=="call":return spec[1] in ("isinstance","bool")
    return False

def supported(node,allow_ordering=False):
    # Constructed containers and interned scalar constants are indistinguishable
    # by value in the trace. Reserve identity checks for actual object references.
    for sub in ast.walk(node):
        if isinstance(sub,ast.Set) and not all(isinstance(e,ast.Constant) for e in sub.elts):return False
        if isinstance(sub,ast.Compare) and any(isinstance(op,(ast.Is,ast.IsNot)) for op in sub.ops):
            from branch_model import spec
            if not all(simple_identity(spec(value)) for value in [sub.left,*sub.comparators]):return False
        if not allow_ordering and isinstance(sub,ast.Compare) and any(isinstance(op,(ast.Lt,ast.Gt,ast.LtE,ast.GtE)) for op in sub.ops):
            # Primitive and object comparisons have different ordering protocols.
            # The prior numeric-comparison profile handles supported inequalities.
            return False
    return True

@lru_cache(8)
def change(selector,text,allow_ordering=False):
    edit=possible_edit(selector,text)
    if not edit:return None
    path,new,mask=edit
    start=(mask&-mask).bit_length()-1;end=mask.bit_length()-1
    def snippet(source):
        lines=source.splitlines()[start-1:end]
        if not lines or not lines[0][:1].isspace():raise ValueError
        if lines[-1].rstrip().endswith(":"):
            lines.append(lines[0][:len(lines[0])-len(lines[0].lstrip())]+"    pass")
        return ast.parse("if True:\n"+"\n".join(lines)).body[0].body
    try:a,b=snippet(selector.SRC[path]),snippet(new)
    except (SyntaxError,ValueError,IndexError):return None
    wrap=lambda nodes:ast.Module(body=nodes,type_ignores=[])
    aa,bb=wrap(a),wrap(b)
    cond=lambda n:[v for v in ast.walk(n) if isinstance(v,(ast.If,ast.While,ast.IfExp))]
    source=cond(aa);updated=cond(bb)
    if len(source)!=len(updated):return None
    for left,right in zip(source,updated):
        old,new_node=left.test,right.test
        if ast.dump(old)==ast.dump(new_node):continue
        if not supported(old,allow_ordering) or not supported(new_node,allow_ordering):continue
        x=[];y=[]
        before,after=tree(old,x),tree(new_node,y)
        if not 1<=len(x)<=6 or x!=y or before==after:continue
        # Require the entire semantic edit to be inside this one conditional.
        left.test=new_node
        equal=ast.dump(aa)==ast.dump(bb)
        left.test=old
        if not equal:continue
        return (path,start+old.lineno-2,old.col_offset),before,after,len(x)
    return None

def refine(selector,text,selected):
    if not selected:return selected
    proof=change(selector,text)
    if proof is None:return selected
    site,before,after,leaves=proof
    names,sites=facts()
    if set(names)!=set(selector.NUM):return selected
    rows=sites.get(site)
    if rows is None:return selected
    may=0
    for state,tests in rows.items():
        if state is None or len(state)!=leaves or not unchanged(before,after,state):may|=tests
    path=site[0]
    protected=preserve(selector,text,selected)|selector.tests_of(selector.M["opens"].get(path))
    protected.update(i for i,(opened,_) in selector.EXTRA.items() if path in opened)
    return {i for i in selected if i in protected or may&names[selector.TEST_FILE_ID[i]]}
