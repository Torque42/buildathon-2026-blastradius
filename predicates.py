"""Instrument source expressions on the pristine pin; describe equivalent AST edits."""
import ast
import operator

OPS = {"Eq":operator.eq,"NotEq":operator.ne,"Is":operator.is_,"IsNot":operator.is_not,
       "Lt":operator.lt,"LtE":operator.le,"Gt":operator.gt,"GtE":operator.ge}
OP_NAMES=list(OPS)

def location(path,node,kind):
    return f"{path}:{node.lineno}:{node.col_offset}:{kind}"

def scalar(value):
    return value is None or type(value) in (str,int,float,bool)

class Probes(ast.NodeTransformer):
    def __init__(self,path):
        self.path=path
        self.inside=False
        self.sites=set()

    def visit_FunctionDef(self,node):
        prev=self.inside;self.inside=True
        node.body=[self.visit(s) for s in node.body]
        self.inside=prev
        return node
    visit_AsyncFunctionDef=visit_FunctionDef

    def visit_ClassDef(self,node):
        prev=self.inside;self.inside=False
        node.body=[self.visit(s) for s in node.body]
        self.inside=prev
        return node

    def site(self,node,kind):
        key=location(self.path,node,kind)
        self.sites.add(key)
        return ast.Constant(key)

    def wrap(self,node,kind):
        probe=ast.Call(ast.Name("_br_seen",ast.Load()),[self.site(node,kind)],[])
        return ast.copy_location(ast.BoolOp(ast.Or(),[probe,node]),node)

    def visit_Compare(self,node):
        self.generic_visit(node)
        if not self.inside or len(node.ops)!=1 or type(node.ops[0]).__name__ not in OPS:return node
        a,b=node.left,node.comparators[0]
        which=(1 if isinstance(a,ast.Constant) and scalar(a.value) else
               2 if isinstance(b,ast.Constant) and scalar(b.value) else 0)
        return ast.copy_location(ast.Call(ast.Name("_br_compare",ast.Load()),
            [self.site(node,"cmp"),ast.Constant(type(node.ops[0]).__name__),ast.Constant(which),a,b],[]),node)

    def visit_BoolOp(self,node):
        self.generic_visit(node)
        return self.wrap(node,"bool") if self.inside else node

    def visit_UnaryOp(self,node):
        self.generic_visit(node)
        return self.wrap(node,"not") if self.inside and isinstance(node.op,ast.Not) else node

    def visit_Call(self,node):
        self.generic_visit(node)
        def literal(n):
            return self.wrap(n,"literal") if self.inside and isinstance(n,ast.Constant) and scalar(n.value) else n
        node.args=[literal(n) for n in node.args]
        for kw in node.keywords:kw.value=literal(kw.value)
        return node

    def visit_If(self,node):
        orig=node.test
        self.generic_visit(node)
        if self.inside:
            probe=ast.Call(ast.Name("_br_seen",ast.Load()),[self.site(orig,"condition")],[])
            node.test=ast.copy_location(ast.BoolOp(ast.Or(),[probe,node.test]),orig)
        return node
    visit_While=visit_If
    visit_IfExp=visit_If
