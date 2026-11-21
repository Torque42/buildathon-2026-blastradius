"""Attribute SQL fixture opens to the exact pristine reader and its pytest context."""
import atexit
import gzip
import json
import os
import sys
from pathlib import Path

active=os.environ.get("BR_FLAG_TEST","")
root=os.path.realpath(os.environ["BR_ROOT"])
prefix=root+"/tests/fixtures/"
helpers=root+"/tests/helpers.py"
events={}
def observed(event,args):
    if event!="open" or not isinstance(args[0],str):return
    filename=args[0]
    if not os.path.isabs(filename):filename=os.path.abspath(filename)
    if not filename.startswith(prefix) or not filename.endswith(".sql"):return
    frame=sys._getframe(1)
    mode=frame.f_code.co_name if frame.f_code.co_filename==helpers else "raw"
    events.setdefault(filename[len(root)+1:],{}).setdefault(mode,set()).add(active)
def save():
    payload={file:{k:sorted(v) for k,v in modes.items()} for file,modes in events.items()}
    (Path(os.environ["BR_FLAG_DIR"])/(str(os.getpid())+".json.gz")).write_bytes(gzip.compress(json.dumps([[],payload]).encode(),mtime=0))
sys.addaudithook(observed);atexit.register(save)
