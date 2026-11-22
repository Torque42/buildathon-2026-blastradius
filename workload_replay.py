"""Validate complete test workloads by replaying their affected calls on the patched source."""

from collections import defaultdict
from functools import lru_cache
import gzip
import hashlib
import dis
import marshal
import os
from pathlib import Path
import resource
import signal
import sys
import tempfile
from types import CodeType

import function_changes
import instruction_impact as impact
import query_phases
import query_replay
import replay_edit
import shortlist

ROOT = Path(__file__).resolve().parent
NODE = "tests/test_parser.py::TestParser::test_max_nodes"
SPENT = 0.0
PERSISTENT_OPERATIONS = {
    "setattr", "delattr", "environ", "modules", "setrecursionlimit",
    "settrace", "setprofile", "globals", "locals", "exec", "eval", "open", "sys", "os",
    "random", "secrets", "signal", "register", "unregister", "cache_clear",
}


def cpu():
    return sum(r.ru_utime + r.ru_stime for r in (
        resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN)))


@lru_cache(1)
def node_profile():
    version, calls, stages = marshal.loads(gzip.decompress((ROOT / "maps/workload-node.gz").read_bytes()))
    by_member = defaultdict(lambda: defaultdict(dict))
    labels = list(stages)
    for i, label in enumerate(labels):
        for (path, name, line, digest), visits in stages[label].items():
            rows = by_member[path, name, line][digest]
            rows[visits] = rows.get(visits, 0) | (1 << i)
    return version, calls, labels, by_member


def children(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, CodeType):
            yield from children(value)


@lru_cache(12)
def body_edits(path, before, after):
    """Return potential first divergences in existing bodies, or an entire-module dependency."""
    shell, old_funcs, old_codes = function_changes.baseline(path, before)
    other_shell, new_funcs = function_changes.changed_skeleton(path, before, after)
    if (shell != other_shell or old_funcs.keys() != new_funcs.keys()
            or None in old_funcs.values() or None in new_funcs.values()):
        return None
    changed = [name for name, row in old_funcs.items() if row != new_funcs[name]
               and (row is None or new_funcs[name] is None or row[1] != new_funcs[name][1])]
    if any(old_funcs[name] is None or new_funcs[name] is None for name in changed):
        return None
    new_codes = defaultdict(list)
    if changed:
        for _, code in impact.lineage(compile(after, path, "exec", dont_inherit=True)):
            new_codes[code.co_qualname].append(code)
    affected = []
    for name in changed:
        if len(old_codes.get(name, ())) != 1 or len(new_codes.get(name, ())) != 1:
            return None
        a, b = old_codes[name][0], new_codes[name][0]
        old_family, new_family = list(children(a)), list(children(b))
        for member in (*old_family, *new_family):
            if PERSISTENT_OPERATIONS.intersection(member.co_names):
                return None
            if any(op.opname in {"STORE_GLOBAL", "DELETE_GLOBAL", "IMPORT_NAME"} or
                   (op.opname in {"STORE_DEREF", "DELETE_DEREF"} and op.argval in member.co_freevars)
                   for op in dis.get_instructions(member)):
                return None
        same_family = [c.co_qualname for c in old_family] == [c.co_qualname for c in new_family]
        for index, code in enumerate(old_family):
            mask = None
            if same_family and len(code.co_code) < 8000:
                mask = impact.offset_mask(code, new_family[index])
            if mask == 0:
                continue
            key = (path, code.co_qualname, code.co_firstlineno,
                   hashlib.blake2s(code.co_code, digest_size=12).digest())
            affected.append((key, -1 if mask is None else mask))
    return affected


def prepare(selector, text):
    """Apply every hunk to the verified pin. An unknown file makes this verifier abstain."""
    edits, details, opaque, fixtures = {}, [], set(), set()
    seen = set()
    for patch in selector.parse_patch(text):
        path = patch["old"]
        if not path or path != patch["new"] or path in seen or not patch["hunks"]:
            return
        seen.add(path)
        if path.startswith("tests/fixtures/") and ".." not in Path(path).parts:
            fixtures.add(path)
            continue
        if not path.startswith("sqlglot/") or not path.endswith(".py") or path not in selector.SRC:
            return
        before = selector.SRC[path]
        lines = before.splitlines()
        for hunk in patch["hunks"]:
            old = [line[1:].rstrip("\r") for line in hunk["lines"] if line.startswith((" ", "-"))]
            i = max(hunk["os"] - 1, 0)
            if lines[i:i + len(old)] != old:
                return
        after, _, _ = selector.apply(before, patch["hunks"])
        edits[path] = after
        result = body_edits(path, before, after)
        if result is None:
            opaque.add(path)
        else:
            details.extend(result)
    if edits:
        return edits, details, opaque, fixtures


def file_readers(selector, paths):
    readers = set()
    for path in paths:
        readers.update(selector.tests_of(selector.M["opens"].get(path)))
        readers.update(n for n, (observed, _) in selector.EXTRA.items() if path in observed)
        lines = selector.profile()["code_reads"].get(path, ())
        if lines:
            readers.update(replay_edit.preserve_scope(selector, path, sum(1 << n for n in set(lines)), selector.ALL))
    return readers


def affected_stages(node, details, opaque, trace, relevant):
    """Cross-check the staged trace against cold pytest, including spawned processes."""
    version, numbers, known, cold = impact.observations()
    number = numbers.get(node, 0)
    if tuple(version) != sys.version_info[:2] or not number & known:
        return
    for path in opaque:
        if path not in cold:
            return
        if any(group & number for groups in cold[path].values() for group in groups.values()):
            return
    result = 0
    for (path, name, line, digest), wanted in details:
        if path not in cold:
            return
        exact = cold[path].get((name, line, digest), {})
        for (other_name, other_line, other_digest), groups in cold[path].items():
            if (name, line) == (other_name, other_line) and digest != other_digest:
                if any(group & number for group in groups.values()):
                    return
        full = 0
        for visits, tests in exact.items():
            if number & tests:
                full |= visits
        footprint = trace.get((path, name, line), {})
        if set(footprint) - {digest}:
            return
        observed = 0
        for visits, stages in footprint.get(digest, {}).items():
            if relevant & stages:
                observed |= visits
                if visits & wanted:
                    result |= relevant & stages
        if full & wanted & ~observed:
            return
    return result


def run_node(edits, reusable):
    """Run the pinned unittest, reusing only identical large calls whose code could not diverge."""
    import unittest
    with tempfile.TemporaryDirectory(prefix="br-node-") as tmp:
        folder = Path(tmp)
        shortlist.patched_tree(folder, edits)
        pid = os.fork()
        if pid == 0:
            status = 1
            try:
                with open(os.devnull, "wb") as sink:
                    os.dup2(sink.fileno(), 1)
                    os.dup2(sink.fileno(), 2)
                signal.setitimer(signal.ITIMER_PROF, 0.85)
                signal.alarm(3)
                sys.path.insert(0, str(folder))
                os.chdir(folder)
                for path, source in edits.items():
                    sys.meta_path.insert(0, replay_edit.Overlay(path, source, folder))
                from tests import test_parser
                from sqlglot.errors import ParseError
                original = test_parser.parse_one
                observed = set()

                def parse(sql, *args, **kwargs):
                    if len(sql) < 100_000:
                        return original(sql, *args, **kwargs)
                    if args:
                        raise RuntimeError("Unknown positional parse arguments")
                    key = (hashlib.sha256(sql.encode()).digest(), tuple(sorted(kwargs.items())))
                    error = reusable.get(key)
                    if not error or error[:2] != ("sqlglot.errors", "ParseError"):
                        raise RuntimeError("Unverified parser call")
                    observed.add(key)
                    raise ParseError(error[2])

                test_parser.parse_one = parse
                result = unittest.TestResult()
                unittest.TestSuite([test_parser.TestParser("test_max_nodes")]).run(result)
                if result.wasSuccessful() and result.testsRun == 1 and not result.skipped and observed == set(reusable):
                    status = 0
            finally:
                os._exit(status)
        return os.waitpid(pid, 0)[1] == 0


def verify_node(edits, details, opaque):
    version, calls, labels, trace = node_profile()
    if tuple(version) != sys.version_info[:2]:
        return False
    visited = affected_stages(NODE, details, opaque, trace, (1 << len(labels)) - 1)
    if visited is None:
        return False
    hit = {name for i, name in enumerate(labels) if visited & (1 << i)}
    if "outside" in hit:
        return False
    if not hit:
        return True
    reusable = {}
    for index, (key, length, _, error) in enumerate(calls):
        if length >= 100_000:
            if str(index) in hit or not error:
                return False
            reusable[key] = error
    return bool(reusable) and run_node(edits, reusable)


def executor_jobs(kind, details, opaque):
    labels, trace, kinds = query_phases.profile()
    phases = affected_stages(query_replay.IDS[kind], details, opaque, trace, kinds[kind] | kinds["init"])
    if phases is None or phases & kinds["init"]:
        return
    rows = defaultdict(set)
    while phases:
        bit = phases & -phases
        phases -= bit
        _, index, stage = labels[bit.bit_length() - 1]
        if stage == "harness":
            return
        rows[index].add(stage)
    jobs = []
    for index, stages in sorted(rows.items()):
        rules = sorted(int(s[5:]) for s in stages if s.startswith("rule-"))
        basic = sorted(stages - {s for s in stages if s.startswith("rule-")} - {"outer"})
        if rules or "outer" in stages:
            basic.append("optimize")
        if rules and "outer" not in stages and max(rules) >= len(query_replay.profile()[2][kind][index].get("rules", ())):
            return
        jobs.append((index, basic, None if "outer" in stages else rules))
    return jobs


def refine(selector, text, selected):
    global SPENT
    ids = {n: selector.NUM[name] for n, name in {"node": NODE, **query_replay.IDS}.items()
           if selector.NUM.get(name) in selected}
    if not ids or SPENT >= 18 or "sqlglot" in sys.modules:
        return selected
    possible = (selector.worker_cost(selector.split(selected)) -
                selector.worker_cost(selector.split(selected - set(ids.values())))) / 1000
    if possible < 0.6:
        return selected
    start = cpu()
    try:
        changes = prepare(selector, text)
        if changes is None:
            return selected
        edits, details, opaque, fixtures = changes
        protected = file_readers(selector, set(edits) | fixtures)
        if any(name.startswith("tests/fixtures/optimizer/tpc-") for name in fixtures):
            protected.update(n for kind, n in ids.items() if kind != "node")
        ids = {k: n for k, n in ids.items() if n not in protected}
        passed = set()
        if "node" in ids and verify_node(edits, details, opaque):
            passed.add(ids["node"])
        needed = {}
        for kind in sorted(ids.keys() - {"node"}):
            jobs = executor_jobs(kind, details, opaque)
            if jobs is not None:
                if jobs:
                    needed[kind] = jobs
                else:
                    passed.add(ids[kind])
        if needed:
            budget = min(4.0, possible * 0.38, max(0.02, 18 - SPENT - (cpu() - start)))
            changes = [(p, source, 0) for p, source in edits.items()]
            done = query_replay.run_checks(changes, needed, budget, sorted(edits))
            passed.update(ids[k] for k in done)
        if passed:
            print("workload verified", ",".join(sorted(k for k, n in ids.items() if n in passed)),
                  file=sys.stderr)
        return selected - passed
    except (ValueError, TypeError, IndexError, KeyError, OSError, SyntaxError, RecursionError):
        return selected
    finally:
        SPENT += cpu() - start
