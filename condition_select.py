import ast
from functools import lru_cache
import gzip,marshal
from pathlib import Path
from conditions import compile_condition,plan
from replay_edit import possible_edit

@lru_cache(1)
def metadata():
    catalog,sites=marshal.loads(gzip.decompress((Path(__file__).parent/"maps/conditions.gz").read_bytes()))
    return {n:1<<i for i,n in enumerate(catalog)},sites


def truth(expression,state):
    op=expression[0]
    if op=="Not":return not truth(expression[1],state)
    if op=="And":return all(truth(child,state) for child in expression[1])
    if op=="Or":return any(truth(child,state) for child in expression[1])
    _,index,comp=expression;raw=state[index]
    if comp=="truth":return raw=="T"
    identity=raw[0]=="1";rel=raw[1:]
    if comp=="Is":return identity
    if comp=="IsNot":return not identity
    if comp=="Eq":return rel in ("=","U")
    if comp=="NotEq":return rel not in ("=","U")
    if rel in ("U","D"):raise ValueError("unordered")
    return rel in {"Lt":"<","LtE":"<=","Gt":">","GtE":">="}[comp]


def refine(selector,text,selected):
    if not selected:return selected
    edit=possible_edit(selector,text)
    if not edit:return selected
    path,updated,mask=edit
    start=(mask&-mask).bit_length()-1;end=mask.bit_length()-1
    old=selector.SRC[path].splitlines();new=updated.splitlines()
    before="\n".join(old[start-1:end]);after="\n".join(new[start-1:end])
    indent=before[:len(before)-len(before.lstrip())]
    def parse(s):
        # Preserve original columns and add exactly one line before the snippet.
        if s.rstrip().endswith(":"):s+="\n"+indent+"    pass"
        return ast.parse("if True:\n"+s).body[0].body[0]
    try:
        aa,bb=parse(before),parse(after)
    except (SyntaxError,IndexError):return selected
    bits,sites=metadata()
    a=[n.test for n in ast.walk(aa) if isinstance(n,(ast.If,ast.IfExp,ast.While))]
    b=[n.test for n in ast.walk(bb) if isinstance(n,(ast.If,ast.IfExp,ast.While))]
    if len(a)!=len(b):return selected
    for old_node,new_node in zip(a,b):
        condition=compile_condition(old_node)
        if not condition:continue
        args,first=condition;leaves=[];second=plan(new_node,leaves)
        if not second or first==second or args!=tuple(leaves):continue
        site=f"{path}:{old_node.lineno+start-2}:{old_node.col_offset}"
        if site not in sites:continue
        known,observed=sites[site]
        safe=known
        for state,users in observed.items():
            if not state:safe&=~users;continue
            try:
                if truth(first,state)!=truth(second,state):safe&=~users
            except (KeyError,ValueError,TypeError,IndexError):safe&=~users
        return {n for n in selected if not safe&bits.get(selector.TEST_FILE_ID[n],0)}
    return selected
