"""Find which complete test executions can first encounter any part of a compound change."""
from functools import lru_cache
import re
import sys

import access_select
import collection_guard
import get_inputs
import instruction_impact
import isolated_imports
import replay_edit
import workload_replay


@lru_cache(24)
def members_by_location(path):
    _,_,_,files=instruction_impact.observations()
    members={}
    for (name,line,digest),rows in files.get(path,{}).items():
        members.setdefault((name,line),[]).append((digest,rows))
    return members


@lru_cache(32)
def instructions(path,before,after):
    details=workload_replay.body_edits(path,before,after)
    if details is None:
        return
    version,names,known,files=instruction_impact.observations()
    if tuple(version)!=sys.version_info[:2] or path not in files:
        return
    may=0
    entries=members_by_location(path)
    for (_,name,line,digest),changed in details:
        for variant,groups in entries.get((name,line),()):
            for seen,tests in groups.items():
                if digest!=variant or seen & changed:
                    may|=tests
    return known & ~may


def raw(selector,path,selection):
    readers=selector.tests_of(selector.M["opens"].get(path))
    readers.update(n for n,(files,_) in selector.EXTRA.items() if path in files)
    lines=selector.profile()["code_reads"].get(path,())
    if lines:
        readers.update(replay_edit.preserve_scope(selector,path,sum(1<<n for n in set(lines)),selection))
    return readers & selection


def independent_hunks(path,patch,lines):
    """Build individual replacements against the same immutable source coordinates."""
    changes=[]
    end=-1
    for h in patch["hunks"]:
        pos=max(0,h["os"]-1)
        context=[s[1:].rstrip("\r") for s in h["lines"] if s.startswith((" ","-"))]
        if pos<end or lines[pos:pos+len(context)]!=context:
            return
        end=pos+len(context)
        group=[]
        for item in [*h["lines"]," "]:
            if item.startswith(("-","+")):
                group.append(item.rstrip("\r"))
                continue
            if not item.startswith(" "):
                continue
            if group:
                a=sum(t.startswith("-") for t in group)
                b=len(group)-a
                # New/removed statements or changed lexical scope must be compared
                # as one compiled file. Separate only ordinary line replacements.
                if a!=1 or b!=1:
                    return
                left,right=group[0][1:],group[1][1:]
                if (len(left)-len(left.lstrip())!=len(right)-len(right.lstrip()) or
                        any(ch in left+right for ch in ("\\","'''",'"""'))):
                    return
                changes.append((pos,left,right))
                pos+=1
                group=[]
            pos+=1
    if 1<=len(changes)<=8:
        return ["diff --git a/{0} b/{0}\n--- a/{0}\n+++ b/{0}\n@@ -{1},1 +{1},1 @@\n-{2}\n+{3}\n".format(
                path,pos+1,left,right) for pos,left,right in changes]


def pure_lookup(selector,text,selected):
    # These two observers prove equal values (including aliases) for plain
    # builtins. Their lookups cannot run patched user code between the two values.
    evidence=get_inputs.refine(selector,text,selected)
    if evidence is selected:
        evidence=access_select.refine(selector,text,selected)
    return evidence


def piece(selector,path,form,text,selected,split):
    if path in selector.profile()["source_reads"]:
        return selected
    before=selector.SRC[path]
    after,removed,added=selector.apply(before,form["hunks"])
    if not removed and not added:
        return set()
    # A change to a callable's execution protocol can be visible to its caller
    # before the body executes. Preserve these cases for the full selector.
    words=set(re.findall(r"\w+","\n".join(s for h in form["hunks"] for s in h["lines"]
                                        if s.startswith(("-","+")))))
    if words & {"yield","await","async","nonlocal","global","__code__","__getattribute__","__getattr__"}:
        return selected
    direct=instructions(path,before,after)
    keep=selected
    if direct is not None:
        _,names,_,_=instruction_impact.observations()
        keep={n for n in selected if not direct & names.get(selector.TEST_FILE_ID[n],0)}
    atoms=independent_hunks(path,form,before.splitlines()) if split else None
    if atoms and len(atoms)>1:
        component=set()
        for atom in atoms:
            change=selector.parse_patch(atom)[0]
            subset=piece(selector,path,change,atom,selected,False)
            component.update(subset)
            if component==selected:
                break
        keep=keep & component
    elif len(removed)==1 and len(added)==1:
        keep=keep & pure_lookup(selector,text,selected)
    if keep==selected:
        return selected & isolated_imports.readers(selector,path)
    return keep | raw(selector,path,selected)


def refine(selector,text,selected):
    if len(selected)<30 or not text:
        return selected
    parts=re.split(r"(?m)(?=^diff --git )",text)
    parts=[p for p in parts if p.startswith("diff --git ")]
    if not 1<=len(parts)<=12 or len(parts)==1 and text.count("\n@@ ")<2:
        return selected
    all_paths=set()
    union=set()
    for part in parts:
        parsed=selector.parse_patch(part)
        if len(parsed)!=1:
            return selected
        form=parsed[0]
        path=form["old"]
        if not path or path!=form["new"] or path in all_paths or not form["hunks"]:
            return selected
        all_paths.add(path)
        if path.startswith("tests/fixtures/") and path.endswith(".sql"):
            from fixture_inputs import profiles,expand
            uses=profiles()[0]["readers"].get(path)
            if not uses:
                return selected
            by_mode=set().union(*(set(c) for c in uses.values()))
            readers=expand(selector,by_mode) | raw(selector,path,selected) | selector.analyze(part)[0]
        elif path.startswith("sqlglot/") and path in selector.SRC:
            lines=selector.SRC[path].splitlines()
            for h in form["hunks"]:
                i=max(0,h["os"]-1)
                previous=[s[1:].rstrip("\r") for s in h["lines"] if s.startswith((" ","-"))]
                if lines[i:i+len(previous)]!=previous:
                    return selected
            readers=piece(selector,path,form,part,selected,True)
        else:
            return selected
        union.update(readers)
        if selected<=union:
            return selected
    return selected & union
