"""Identify reads from built-in sequences and string-keyed mappings."""
import ast

def number(node):
    sign=1
    if isinstance(node,ast.UnaryOp) and isinstance(node.op,ast.USub):node=node.operand;sign=-1
    if isinstance(node,ast.Constant) and type(node.value) is int and abs(node.value)<=64:return sign*node.value

def recognize(node):
    if isinstance(node,ast.Call) and not node.keywords:
        if isinstance(node.func,ast.Name) and node.func.id=="seq_get" and len(node.args)==2 and isinstance(node.args[0],ast.Name):
            arg=node.args[1];value=number(arg)
            if value is not None:return "seq",node.args[0],arg,value
        if isinstance(node.func,ast.Attribute) and node.func.attr=="get" and len(node.args) in (1,2):
            arg=node.args[0]
            if isinstance(arg,ast.Constant) and type(arg.value) is str and len(arg.value)<=80:
                return "get" if len(node.args)==1 else "get-default",node.func.value,arg,arg.value
    elif isinstance(node,ast.Subscript) and isinstance(node.ctx,ast.Load):
        value=number(node.slice)
        if value is not None:return "index",node.value,node.slice,value

def entries(path,tree):
    result={}
    for node in ast.walk(tree):
        hit=recognize(node)
        if not hit:continue
        kind,_,arg,value=hit
        if arg.lineno==arg.end_lineno:
            result.setdefault(arg.lineno,[]).append((arg.col_offset,arg.end_col_offset,kind,value))
    return result
