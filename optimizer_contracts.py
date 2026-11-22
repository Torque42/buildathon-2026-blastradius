"""Verify a changed optimizer on complete captured rule inputs for expensive SQL tests."""
import re
import resource
import sys

import function_changes
import instruction_impact
import query_phases
import query_replay
from replay_edit import preserve_scope


BLOCK_WORDS = re.compile(r"\b(open|exec|eval|globals|locals|vars|setattr|delattr|print|logger|warnings|sys|os|random|time|global|nonlocal|import|yield|await)\b|__")
spent = 0.0


def cpu():
    return sum(r.ru_stime+r.ru_utime for r in (resource.getrusage(resource.RUSAGE_SELF),
                                               resource.getrusage(resource.RUSAGE_CHILDREN)))


def functions(selector, patch):
    path = patch["old"]
    if (not path or patch["new"] != path or not path.startswith("sqlglot/optimizer/")
            or path not in selector.SRC or path in selector.profile()["source_reads"]):
        return
    lines = [line[1:] for h in patch["hunks"] for line in h["lines"] if line.startswith(("+","-"))]
    if not lines or len(lines)>100 or any(BLOCK_WORDS.search(line) for line in lines):
        return
    old = selector.SRC[path]
    new, _, _ = selector.apply(old,patch["hunks"])
    shell, before, _ = function_changes.baseline(path,old)
    new_shell, after = function_changes.skeleton(new)
    if shell!=new_shell or before.keys()!=after.keys() or None in before.values() or None in after.values():
        return
    changed = {name for name in before if before[name][1]!=after[name][1]}
    if not changed:
        return
    return path, new, changed


def nodes_in(mask):
    while mask:
        bit = mask & -mask
        yield bit.bit_length()-1
        mask -= bit


def work(selector, patch, ids):
    change = functions(selector,patch)
    if not change:
        return
    path, new, changed = change
    version, names, known, cold = instruction_impact.observations()
    if tuple(sys.version_info[:2])!=tuple(version) or path not in cold:
        return
    labels, phases, kinds = query_phases.profile()
    allowed = {k for k in ids if names.get(query_replay.IDS[k],0) & known}
    raw = selector.tests_of(selector.M["opens"].get(path)) | {n for n,(fs,_) in selector.EXTRA.items() if path in fs}
    seen_lines = set(selector.profile()["code_reads"].get(path,()))
    raw |= preserve_scope(selector,path,sum(1<<n for n in seen_lines),set(ids.values()))
    allowed = {k for k in allowed if ids[k] not in raw}
    stages = 0
    def modified(name):
        return any(name==fn or name.startswith(fn+".<locals>.") for fn in changed)
    for (filename,name,_), signatures in phases.items():
        if filename==path and modified(name):
            for offsets in signatures.values():
                for mask in offsets.values():
                    stages |= mask
    # Trace the whole changed function and its closures. This includes unchanged
    # instructions, so a new path taken inside the function still gets replayed.
    for (name,line,digest), groups in cold[path].items():
        if not modified(name):
            continue
        relevant = {k for k in allowed if any(mask & names[query_replay.IDS[k]] for mask in groups.values())}
        if not relevant:
            continue
        entries = phases.get((path,name,line),{}).get(digest,{})
        for k in relevant:
            seen = 0
            full = 0
            for offsets,tests in groups.items():
                if tests & names[query_replay.IDS[k]]:
                    full |= offsets
            for offsets,visited in entries.items():
                if visited & (kinds[k]|kinds["init"]):
                    seen |= offsets
                    stages |= visited & (kinds[k]|kinds["init"])
            if full & ~seen:
                allowed.discard(k)
    if stages & kinds["init"]:
        return
    jobs = {k:{} for k in allowed}
    for number in nodes_in(stages):
        kind, index, stage = labels[number]
        if kind not in jobs:
            continue
        if stage in {"harness","prepare","finish","plan","execute_plan"}:
            del jobs[kind]
            continue
        jobs[kind].setdefault(index,set()).add(stage)
    result = {}
    for kind, rows in jobs.items():
        result[kind] = []
        for i, parts in sorted(rows.items()):
            rules = sorted(int(p[5:]) for p in parts if p.startswith("rule-"))
            basic = sorted(parts & {"parse","reference"})
            if rules or "outer" in parts:
                basic.append("optimize")
            result[kind].append((i,basic,None if "outer" in parts else rules))
    return (path,new,0), result


def refine(selector, text, selected):
    global spent
    if spent>=7.0 or "sqlglot/optimizer/" not in text:
        return selected
    ids = {k:selector.NUM[node] for k,node in query_replay.IDS.items() if selector.NUM.get(node) in selected}
    if not ids:
        return selected
    base = selector.worker_cost(selector.split(selected))
    ids = {k:n for k,n in ids.items() if base-selector.worker_cost(selector.split(selected-{n}))>=6500}
    if not ids:
        return selected
    patches = selector.parse_patch(text)
    if len(patches)!=1:
        return selected
    start = cpu()
    try:
        data = work(selector,patches[0],ids)
        if data is None:
            return selected
        edit, rows = data
        keep = {k for k,jobs in rows.items() if not jobs}
        pending = {k:jobs for k,jobs in rows.items() if jobs}
        if pending:
            keep.update(query_replay.run_checks([edit],pending,min(5.5,7.0-spent),()))
        return selected-{ids[k] for k in keep}
    finally:
        spent += max(0,cpu()-start)
