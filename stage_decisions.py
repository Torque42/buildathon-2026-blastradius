"""Remove stages only when every observed decision and its remaining effects agree."""
import gzip
import marshal
from functools import lru_cache
from pathlib import Path

import branch_select
from branch_model import unchanged

@lru_cache(1)
def profile():
    return marshal.loads(gzip.decompress((Path(__file__).parent/"maps/stage-decisions.gz").read_bytes()))

def unchanged_stages(selector,text,labels,selected):
    if not selected:return 0
    change=branch_select.change(selector,text,allow_ordering=True)
    if not change:return 0
    site,before,after,arity=change
    timeline,data=profile()
    if timeline!=labels or site not in data:return 0
    known,states=data[site]
    safe=selected & known
    for record,mask in states.items():
        if safe&mask and (len(record)!=arity or not unchanged(before,after,record)):
            safe &= ~mask
    return safe
