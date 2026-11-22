"""Replay unchanged worker results at the real serialization boundary of optimizer tests."""

from collections import defaultdict
import hashlib
import marshal
from pathlib import Path
import pickle
import sys
import zlib


def key(method,packet):
    return method,hashlib.sha256(packet).digest()


def allowed_path(path):
    return (path in ("sqlglot/parser.py","sqlglot/generator.py") or
            path.startswith(("sqlglot/optimizer/","sqlglot/parsers/","sqlglot/generators/")))


class Capture:
    def __init__(self,root):
        self.roots=tuple(dict.fromkeys((str(root.absolute())+"/",str(root.resolve())+"/")))
        self.sites={}
        self.codes={}
        self.by_code={}
        self.visits=defaultdict(dict)
        self.opens={}
        self.keys={}
        self.outputs=[]
        self.by_test=defaultdict(list)
        self.active=None
        self.tool=4
        mon=sys.monitoring
        mon.use_tool_id(self.tool,"optimizer worker observations")
        mon.register_callback(self.tool,mon.events.PY_START,self.start)
        mon.register_callback(self.tool,mon.events.PY_RESUME,self.start)
        mon.register_callback(self.tool,mon.events.LINE,self.line)
        mon.set_events(self.tool,mon.events.PY_START|mon.events.PY_RESUME)
        sys.addaudithook(self.audit)

    def start(self,code,offset):
        ident=id(code)
        index=self.by_code.get(ident)
        if index is None:
            file=code.co_filename
            prefix=next((root for root in self.roots if file.startswith(root)),None)
            if prefix is None or not allowed_path(file[len(prefix):]):
                return sys.monitoring.DISABLE
            path=file[len(prefix):]
            site=(path,code.co_qualname,code.co_firstlineno,hashlib.blake2s(code.co_code,digest_size=12).digest())
            index=self.sites.setdefault(site,len(self.sites))
            self.by_code[ident]=index
            self.codes[ident]=code
            sys.monitoring.set_local_events(self.tool,code,sys.monitoring.events.LINE)
        if self.active is not None:
            self.active[0].add((index,-1))

    def line(self,code,lineno):
        if self.active is not None:
            self.active[0].add((self.by_code[id(code)],lineno-code.co_firstlineno))

    def audit(self,event,args):
        if self.active is not None and event=="open" and isinstance(args[0],str):
            filename=str(Path(args[0]).absolute())
            for root in self.roots:
                if filename.startswith(root):
                    self.active[1].add(filename[len(root):])
                    break

    def capture(self,method,packet,compute):
        visits,opens=set(),set()
        self.active=visits,opens
        try:
            output=compute(packet)
        finally:
            self.active=None
        k=key(method,packet)
        index=self.keys.setdefault(k,len(self.outputs))
        if index==len(self.outputs):
            self.outputs.append(zlib.compress(output,2))
        elif self.outputs[index] is not None and zlib.decompress(self.outputs[index])!=output:
            self.outputs[index]=None
        self.by_test[method].append(index)
        bit=1<<index
        for site,line in visits:
            points=self.visits[site]
            points[line]=points.get(line,0)|bit
        for file in opens:
            self.opens[file]=self.opens.get(file,0)|bit
        return output

    def finish(self):
        sys.monitoring.set_events(self.tool,0)
        for code in self.codes.values():
            sys.monitoring.set_local_events(self.tool,code,0)
        sys.monitoring.free_tool_id(self.tool)
        return (sys.version_info[:2],self.sites,dict(self.visits),self.opens,self.keys,self.outputs,dict(self.by_test))


def compile_impact(selector,edits,profile):
    import function_changes
    import workload_replay
    if not profile or tuple(profile[0])!=sys.version_info[:2]:
        return
    _,sites,visits,opens,_,_,_=profile
    affected=0
    for path,new in edits.items():
        affected|=opens.get(path,0)
        if path.startswith("tests/fixtures/"):
            continue
        if not allowed_path(path):
            return
        old=selector.SRC[path]
        changes=workload_replay.body_edits(path,old,new)
        if changes is None:
            return
        by_name=function_changes.baseline(path,old)[2]
        for site,mask in changes:
            index=sites.get(site)
            if index is None:
                # A differently compiled copy might execute under this qualified name.
                if any(other[:3]==site[:3] for other in sites):
                    return
                continue
            lines=visits.get(index,{})
            if mask<0 or mask&1:
                affected|=lines.get(-1,0)
                continue
            codes=[c for c in by_name.get(site[1],()) if c.co_firstlineno==site[2]]
            if len(codes)!=1:
                affected|=lines.get(-1,0)
                continue
            for start,end,line in codes[0].co_lines():
                a,b=start//2,end//2
                if mask & (((1<<(b-a))-1)<<a):
                    affected|=lines.get(line-site[2] if line is not None else -1,0)
    return affected


class Replay:
    def __init__(self,profile,affected):
        self.keys=profile[4]
        self.outputs=profile[5]
        self.affected=affected
        self.hits=0

    def capture(self,method,packet,compute):
        i=self.keys.get(key(method,packet))
        if i is not None and not self.affected & (1<<i) and self.outputs[i] is not None:
            self.hits+=1
            return zlib.decompress(self.outputs[i])
        return compute(packet)
