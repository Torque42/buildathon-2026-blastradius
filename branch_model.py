"""Short-circuit decision trees. Evaluation stops at an unverified operation."""
import ast
from functools import lru_cache

def spec(node):
    if isinstance(node,ast.Name):return ("name",node.id)
    if isinstance(node,ast.Constant) and type(node.value) in (type(None),bool,int,str,float):return ("constant",node.value)
    if isinstance(node,ast.Attribute):return ("attr",spec(node.value),node.attr)
    if isinstance(node,(ast.Tuple,ast.List,ast.Set)) and len(node.elts)<10:
        return ("tuple",tuple(spec(n) for n in node.elts))
    if isinstance(node,ast.Call) and not node.keywords:
        if isinstance(node.func,ast.Name) and node.func.id in ("len","isinstance","bool") and len(node.args) in (1,2):
            return ("call",node.func.id,tuple(spec(n) for n in node.args))
        if isinstance(node.func,ast.Attribute) and node.func.attr=="get" and len(node.args) in (1,2):
            return ("get",spec(node.func.value),tuple(spec(n) for n in node.args))
    return ("opaque",ast.dump(node,include_attributes=False))

def tree(node,leaves,yes=True,no=False):
    if isinstance(node,ast.UnaryOp) and isinstance(node.op,ast.Not):
        return tree(node.operand,leaves,no,yes)
    if isinstance(node,ast.BoolOp):
        rest=tree(node.values[-1],leaves,yes,no)
        for child in reversed(node.values[:-1]):
            rest=tree(child,leaves,rest,no) if isinstance(node.op,ast.And) else tree(child,leaves,yes,rest)
        return rest
    if isinstance(node,ast.Compare) and len(node.ops)==1:
        key=("compare",spec(node.left),spec(node.comparators[0]))
        op=type(node.ops[0]).__name__
    else:
        key=("truth",spec(node));op="truth"
    if key not in leaves:leaves.append(key)
    return (leaves.index(key),op,yes,no)

def compare(op,record):
    if op=="truth":return record=="T" if record in ("F","T") else None
    if record=="?":return None
    ident,relation,member=record
    if op=="Is":return ident=="1"
    if op=="IsNot":return ident=="0"
    if op in ("In","NotIn"):
        return (member=="1") != (op=="NotIn") if member!="?" else None
    if relation=="?":return None
    if op=="Eq":return relation in ("=","U","I")
    if op=="NotEq":return relation not in ("=","U","I")
    if relation in ("D","U","I"):return None
    allowed={"Lt":"<","LtE":"<=","Gt":">","GtE":">="}
    return relation in allowed[op] if op in allowed else None

@lru_cache(12000)
def unchanged(a,b,state):
    def walk(n):
        while type(n) is not bool:
            i,op,y,z=n
            verdict=compare(op,state[i])
            if verdict is None:break
            n=y if verdict else z
        return n
    # After an opaque operation the incoming frame may be different. Equal
    # residual programs evaluate the same subsequent operands in the same order.
    return walk(a)==walk(b)
