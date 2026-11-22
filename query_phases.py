"""Find the exact query stages that reached instructions changed by a patch."""
import ast
import gzip
import hashlib
import marshal
from functools import lru_cache
from pathlib import Path

import stage_decisions
import default_inputs
import instruction_impact
import query_replay
import replay_edit

@lru_cache(1)
def profile():
    raw = (Path(__file__).parent / "maps/query-phases.gz").read_bytes()
    labels, members = marshal.loads(gzip.decompress(raw))
    stage_kinds = {}
    for index, label in enumerate(labels):
        stage_kinds[label[0]] = stage_kinds.get(label[0], 0) | (1 << index)
    return labels, members, stage_kinds

def literal_default(selector, path, before, after, line):
    for start,end,owner,typ in default_inputs.declarations(selector,path).get(line, ()):
        if not before.isascii() or not after.startswith(before[:start]) or not after.endswith(before[end:]):
            continue
        try:
            new_value = ast.parse(after[start:len(after)-len(before[end:])],mode="eval").body
        except (ValueError,SyntaxError):
            continue
        if not isinstance(new_value, ast.Constant) or type(new_value.value) is not typ:
            continue
        catalog, audited, _, _ = default_inputs.observations()
        if catalog != set(selector.NUM) or f"{path}:{owner}" in audited:
            return None
        for code in instruction_impact.original(path,selector.SRC[path]).values():
            if code.co_firstlineno == owner and code.co_flags & 1 and code.co_name != "<lambda>":
                identity = code.co_qualname, owner, hashlib.blake2s(code.co_code,digest_size=12).digest()
                return [(identity, 1)]
    return None

def analyze(selector,text):
    patch_list = selector.parse_patch(text)
    if len(patch_list) != 1:
        return
    patch = patch_list[0]
    path = patch["old"]
    if not path or path != patch["new"] or path not in selector.SRC or not path.startswith("sqlglot/"):
        return
    if path in selector.profile()["source_reads"] or path in {"sqlglot/optimizer/optimizer.py","sqlglot/serde.py","sqlglot/executor/__init__.py"}:
        return
    before = selector.SRC[path]
    after, removed, added = selector.apply(before,patch["hunks"])
    if len(removed) != 1 or len(added) != 1:
        return
    line = removed[0]
    if selector.recursive_comparison_edit(path,before,after,removed,added):
        return
    aa, bb = before.splitlines(), after.splitlines()
    if aa[:line-1] != bb[:line-1] or aa[line:] != bb[line:]:
        return
    a,b = aa[line-1],bb[line-1]
    identity_swap = any(a.replace(src,dst,1)==b for src,dst in ((" is "," == "),(" is not "," != ")))
    if not (identity_swap or selector.simple_token_change(a,b)):
        return
    hit, details = instruction_impact.single_impact(selector,text)
    if hit is None:
        details = literal_default(selector,path,a,b,line)
        if not details:
            return
        inspect_mask = 1 << line
    else:
        inspect_mask = hit[2]
    return path, after, details, inspect_mask

def refine(selector,text,selected,forbidden=(),attempted=None):
    return refine_parts(selector, [text], selected, forbidden, attempted)


def stage_hits(selector, candidate, ids):
    path, _, changes, inspect_mask = candidate
    protected = replay_edit.preserve_scope(selector,path,inspect_mask,set(ids.values()))
    protected |= selector.tests_of(selector.M["opens"].get(path))
    protected.update(n for n,(raw,_) in selector.EXTRA.items() if path in raw)
    labels, members, stage_kinds = profile()
    _, numbers, known, cold = instruction_impact.observations()
    selected_stages = 0
    blocked = set()
    for (name,line,digest),wanted in changes:
        all_traces = members.get((path,name,line),{})
        if set(all_traces) - {digest}:
            return None
        groups = all_traces.get(digest,{})
        expected = cold.get(path,{}).get((name,line,digest),{})
        if not expected:
            return None
        for visits, stages in groups.items():
            if visits & wanted:
                selected_stages |= stages
        for kind in ids:
            node = query_replay.IDS[kind]
            if not known & numbers.get(node,0):
                blocked.add(kind)
                continue
            # Any cold-test instruction outside our models blocks the exclusion.
            full = 0
            for visits,tests in expected.items():
                if tests & numbers[node]:
                    full |= visits
            seen = 0
            for visits,stages in groups.items():
                if stages & (stage_kinds[kind] | stage_kinds["init"]):
                    seen |= visits
            if wanted & full & ~seen:
                blocked.add(kind)
    return selected_stages, blocked | {kind for kind, number in ids.items() if number in protected}


def refine_parts(selector, parts, selected, forbidden=(), attempted=None):
    ids = {kind: selector.NUM[node] for kind, node in query_replay.IDS.items()
           if selector.NUM.get(node) in selected}
    if not ids or not parts:
        return None
    selected_stages = 0
    blocked = set()
    edits = []
    paths = set()
    for part in parts:
        candidate = analyze(selector, part)
        if candidate is None or candidate[0] in paths:
            return None
        path, source, _, mask = candidate
        paths.add(path)
        hits = stage_hits(selector, candidate, ids)
        if hits is None:
            return None
        stages, unknown = hits
        labels, _, stage_kinds = profile()
        if len(parts) == 1:
            stages &= ~stage_decisions.unchanged_stages(selector, part, labels, stages)
        selected_stages |= stages
        blocked.update(unknown)
        edits.append((path, source, mask))
    if selected_stages & stage_kinds["init"]:
        return None
    # A second edit can affect the input to a later query stage. Replay every
    # reached stage with all patched modules present in the same process.
    jobs = {kind: {} for kind in ids if kind not in blocked}
    while selected_stages:
        bit = selected_stages & -selected_stages
        selected_stages ^= bit
        label = labels[bit.bit_length()-1]
        kind, index, stage = label
        if kind not in jobs:
            continue
        if stage == "harness":
            del jobs[kind]
            continue
        row = jobs[kind].setdefault(index, [])
        row.append(stage)
    if not jobs:
        return None
    _, _, cases = query_replay.profile()
    pending = {}
    safe = set()
    for kind, queries in jobs.items():
        work = []
        for i, stages in sorted(queries.items()):
            normal = [s for s in stages if not s.startswith("rule-") and s != "outer"]
            rules = [int(s[5:]) for s in stages if s.startswith("rule-")]
            if "outer" in stages or rules:
                normal.append("optimize")
            if rules and "outer" not in stages and max(rules) >= len(cases[kind][i].get("rules",())):
                break
            work.append((i, normal, None if "outer" in stages else rules))
        else:
            if work:
                pending[kind] = work
            else:
                safe.add(ids[kind])
    if pending:
        possible = (selector.worker_cost(selector.split(selected-safe)) -
                    selector.worker_cost(selector.split(selected-safe-{ids[k] for k in pending})))/1000
        if possible >= 0.8:
            if attempted is not None:
                attempted.update(ids[kind] for kind in pending)
            verdict = query_replay.run_checks(edits,pending,
                                              query_replay.replay_budget(possible),forbidden)
            safe.update(ids[k] for k in verdict)
    return selected-safe
