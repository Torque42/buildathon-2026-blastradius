"""Portable representation of pure leaves of a Python conditional expression."""
import ast

def argument(node):
    if isinstance(node,ast.Constant) and type(node.value) in (type(None),int,bool,str,float):
        return ("literal",node.value)
    if isinstance(node,ast.Name):return ("name",node.id)
    if isinstance(node,ast.Attribute):
        base=argument(node.value)
        if base:return ("attr",base,node.attr)
    if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=="len" and len(node.args)==1 and not node.keywords:
        base=argument(node.args[0])
        if base:return ("len",base)
    if isinstance(node,ast.Call) and isinstance(node.func,ast.Attribute) and node.func.attr=="get" and len(node.args)==1 and isinstance(node.args[0],ast.Constant):
        base=argument(node.func.value)
        if base:return ("get",base,node.args[0].value)
    return None

def plan(node,leaves):
    if isinstance(node,ast.BoolOp):
        children=[plan(v,leaves) for v in node.values]
        return (type(node.op).__name__,tuple(children)) if all(v is not None for v in children) else None
    if isinstance(node,ast.UnaryOp) and isinstance(node.op,ast.Not):
        child=plan(node.operand,leaves)
        return ("Not",child) if child is not None else None
    if isinstance(node,ast.Compare) and len(node.ops)==1:
        op=type(node.ops[0]).__name__
        if op not in ("Eq","NotEq","Lt","LtE","Gt","GtE","Is","IsNot"):return None
        a,b=argument(node.left),argument(node.comparators[0])
        if not a or not b:return None
        key=("cmp",a,b)
    else:
        a=argument(node)
        if not a:return None
        key=("truth",a);op="truth"
    if key not in leaves:leaves.append(key)
    return ("leaf",leaves.index(key),op)

def compile_condition(node):
    if not isinstance(node,(ast.BoolOp,ast.UnaryOp,ast.Compare)):return None
    leaves=[];tree=plan(node,leaves)
    if tree is not None and len(leaves)<=6:return tuple(leaves),tree
