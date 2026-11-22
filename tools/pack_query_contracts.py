"""Associate pristine binding values with every query and real subprocess harness that reads them."""

from collections import Counter,defaultdict
import dis
import gzip
import hashlib
import lzma
import marshal
from pathlib import Path
import sys
import types

sys.path.insert(0,str(Path(__file__).resolve().parent))
import binding_state
import query_state


def codes(code):
    yield code
    for x in code.co_consts:
        if type(x) is types.CodeType:
            yield from codes(x)


def main():
    root, phases_path, owner_path, harness_path, destination=map(Path,sys.argv[1:])
    labels, coverage=marshal.loads(gzip.decompress(phases_path.read_bytes()))
    version, descriptors, sites=marshal.loads(gzip.decompress(owner_path.read_bytes()))
    harness=marshal.loads(gzip.decompress(harness_path.read_bytes()))
    by_label={name:1<<i for i,name in enumerate(labels)}
    used=defaultdict(int)
    for scope,indexes in sites.items():
        bit=by_label[scope]
        for i in indexes:
            used[descriptors[i]]|=bit
    for kind,ds in harness.items():
        mask=by_label[kind,-1,"harness"]
        for desc in ds:
            used[tuple(desc)]|=mask
    init=by_label["init",]
    uncertain=0
    old={}
    for (path,name,first), variants in coverage.items():
        if path not in old:
            src=root/path
            if not src.is_file():
                continue
            old[path]={(c.co_qualname,c.co_firstlineno,hashlib.blake2s(c.co_code,digest_size=12).digest()):c
                       for c in codes(compile(src.read_bytes(),path,"exec",dont_inherit=True))}
        module=path[:-3].replace("/",".").removesuffix(".__init__")
        for sha, groups in variants.items():
            c=old[path].get((name,first,sha))
            masks=0
            for batch in groups.values():masks|=batch
            if not (masks & ~init):
                continue
            if c is None:
                uncertain |= masks & ~init
                continue
            for op in dis.get_instructions(c):
                if op.opname not in {"LOAD_GLOBAL","LOAD_NAME","LOAD_FROM_DICT_OR_GLOBALS"}:
                    continue
                bit=1<<(op.offset//2)
                entry=(module,"",op.argval,"",None)
                for visits,batches in groups.items():
                    if visits & bit:
                        used[entry]|=batches & ~init
    sys.path.insert(0,str(root))
    names=sorted({k[0] for k in used if k[0]!="*"} | {k[4][1] for k in used if k[4] and k[4][0] in {"type","enum"}})
    query_state.load_modules(names)
    finger=binding_state.Fingerprints()
    values=[]
    unknown=Counter()
    resolved, key_cache={},{}
    for desc,mask in sorted(used.items(), key=lambda pair:repr(pair[0])):
        if not mask:
            continue
        expected=None
        if desc[0]!="*":
            try:
                expected=query_state.projected(desc,finger,resolved,key_cache)
            except (KeyError,ValueError,AttributeError,TypeError,RecursionError) as exc:
                unknown[str(exc)]+=1
        values.append((desc,expected,mask))
    result=version,labels,names,uncertain,values
    destination.write_bytes(lzma.compress(marshal.dumps(result),preset=5))
    print("contexts",len(labels),"bindings",len(values),"unknown stages",uncertain.bit_count(),
          "unresolved values", sum(unknown.values()),unknown.most_common(10), "compressed",destination.stat().st_size,
          file=sys.stderr)


if __name__=="__main__":
    main()
