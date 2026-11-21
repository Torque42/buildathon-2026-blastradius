import atexit,gzip,json,os,sys
from pathlib import Path
from types import FunctionType
active=os.environ.get("BR_TEST","")
root=os.environ["BR_SOURCE"].rstrip("/")+"/"
found={}
busy=False
def audit(event,args):
    global busy
    if busy or event!="object.__getattr__" or args[1]!="__code__" or type(args[0]) is not FunctionType:return
    busy=True
    try:
        code=args[0].__code__;file=code.co_filename
        if not file.startswith(root+"sqlglot/"):return
        frame=sys._getframe(1)
        # CPython's signature builder reads parameter layout, unaffected by a
        # one-token change within an existing ordinary function body.
        if frame.f_globals.get("__name__")=="inspect" and frame.f_code.co_name=="_signature_from_function":return
        key=(file[len(root):],code.co_firstlineno,max((n for _,_,n in code.co_lines() if n is not None),default=code.co_firstlineno))
        found.setdefault(key,set()).add(active)
    finally:busy=False
def save():
    (Path(os.environ["BR_OUT"])/(str(os.getpid())+".json.gz")).write_bytes(gzip.compress(json.dumps([[k,list(v)] for k,v in found.items()]).encode(),mtime=0))
sys.addaudithook(audit)
atexit.register(save)
