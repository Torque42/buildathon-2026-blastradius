"""Compose pinned-state evidence for multiple independent input diffs."""
import re

def pieces(selector,text):
    matches=list(re.finditer(r"(?m)^diff --git ",text))
    if not matches:return []
    pieces=[text[m.start():(matches[i+1].start() if i+1<len(matches) else len(text))] for i,m in enumerate(matches)]
    return pieces if 1<len(pieces)<=5 else []

def select(selector,text,default):
    from replay_edit import possible_edit,preserve
    import query_replay,flag_readers,predicate_select,condition_select
    parts=pieces(selector,text)
    if not parts:return None
    selections=[];edits=[];protected=set();all_function=True;paths=set()
    for part in parts:
        files=selector.parse_patch(part)
        if len(files)!=1:return None
        file=files[0];path=file["old"]
        if not path or path!=file["new"] or path in paths:return None
        paths.add(path)
        sel,reasons=selector.analyze(part)
        edit=possible_edit(selector,part)
        if edit:
            edits.append(edit)
            protected.update(preserve(selector,part,sel))
            sel=predicate_select.refine(selector,part,sel)
            sel=condition_select.refine(selector,part,sel)
        elif path.startswith("tests/fixtures/") or reasons==["comment-only"]:
            all_function=False
        elif path.startswith(("sqlglot/dialects/","sqlglot/parsers/","sqlglot/generators/")) and reasons and all(s.startswith("module-level:") for s in reasons):
            sel=flag_readers.refine(selector,part,sel)
            all_function=False
        else:return None
        selections.append(sel)
    selected=set.union(*selections)
    if all_function and edits:
        selected=query_replay.refine_edits(selector,edits,selected)
    return selected|protected
