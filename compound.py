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

def fixture_mix(selector, text, selected):
    """An untouched fixture cannot be the first changed input observed by a test."""
    import json
    from pathlib import Path
    import fixture_inputs
    import instruction_impact
    import query_replay
    from replay_edit import possible_edit, preserve

    parts = pieces(selector, text)
    if not parts:
        return selected
    source = []
    fixtures = set()
    fixture_tests = set()
    for part in parts:
        files = selector.parse_patch(part)
        if len(files) != 1:
            return selected
        patch = files[0]
        path = patch["old"]
        if path is None or path != patch["new"]:
            return selected
        if path.startswith("sqlglot/"):
            source.append(part)
        elif path.startswith("tests/fixtures/") and path.endswith(".sql"):
            if path not in fixture_inputs.profiles()[0]["contents"] or path in fixtures:
                return selected
            fixtures.add(path)
            fixture_tests.update(selector.analyze(part)[0])
        else:
            return selected
    if len(source) != 1 or not fixtures:
        return selected
    code = source[0]
    edit = possible_edit(selector, code)
    import access_select, branch_select, consumer_select, query_phases
    for path in fixtures:
        subscribers = fixture_inputs.profiles()[0]["readers"][path]
        fixture_tests.update(fixture_inputs.expand(selector, set().union(*(set(ids) for ids in subscribers.values()))))
    protected_inputs = selected & fixture_tests
    possible = selected - protected_inputs
    # If both inputs differ, keep every pristine fixture reader. Other tests
    # can observe their first changed input only through the source edit.
    import identity_select, lexical_inputs, get_inputs, slot_inputs
    reduced = identity_select.refine(selector, code, possible) & possible
    reduced = instruction_impact.refine(selector, code, reduced)
    reduced = slot_inputs.refine(selector, code, reduced)
    reduced = get_inputs.refine(selector, code, reduced)
    lexical = lexical_inputs.refine(selector, code, selector.ALL)
    if lexical is not selector.ALL:
        reduced = lexical - protected_inputs
    reduced = access_select.refine(selector, code, reduced)
    reduced = consumer_select.refine(selector, code, reduced)
    reduced = branch_select.refine(selector, code, reduced)
    result = reduced | protected_inputs
    if not edit:
        return result
    cold_reads = json.loads((Path(__file__).parent / "maps/tpc-fixture-paths.json").read_bytes())
    eligible = {selector.NUM[node] for node, paths in cold_reads.items()
                if node in selector.NUM and not fixtures.intersection(paths)} & result
    if eligible:
        # Keep the full selection when estimating whether the replay pays off.
        # Only explicitly traced executor tests with no reads of these fixtures qualify.
        protected = preserve(selector, code, result)
        attempted = set()
        precise = query_phases.refine(selector, code, result, forbidden=sorted(fixtures), attempted=attempted)
        current = result if precise is None else result - (eligible - precise - protected)
        after = query_replay.refine_edits(selector, [edit], current, forbidden=sorted(fixtures),
                                          skip=attempted)
        result -= eligible - after - protected
    return result
