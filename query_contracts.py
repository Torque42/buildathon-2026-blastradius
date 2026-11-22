"""Check observed runtime dispatch and table entries before replaying changed SQL stages."""

from functools import lru_cache
import hashlib
import lzma
import marshal
import os
import re
import signal
import sys

import binding_state
import instruction_impact
import parser_contract
import query_phases
import query_replay
import replay_edit
import workload_replay

spent = 0.0
errors = (AttributeError, IndexError, ImportError, KeyError, OSError, TypeError, ValueError,
          RecursionError, SyntaxError)


@lru_cache(1)
def profile():
    return marshal.loads(lzma.decompress((binding_state.ROOT / "maps/query-bindings.xz").read_bytes()))


def describe(selector, edits, words):
    if words & (parser_contract.DYNAMIC | workload_replay.PERSISTENT_OPERATIONS):
        return
    details, canonical = [], {}
    for path, after in edits.items():
        before = selector.SRC[path]
        if not parser_contract.namespace_changes(parser_contract.syntax(before).body,
                                                 parser_contract.syntax(after).body):
            return
        old = parser_contract.compiled(path, before)
        new = parser_contract.compiled(path, after)
        module = path[:-3].replace("/", ".").removesuffix(".__init__")
        for name, originals in old.items():
            replacements = new.get(name, ())
            paired = len(originals) == len(replacements)
            for index, original in enumerate(originals):
                # The actual import recreates module/class bodies and their bindings.
                if not original.co_flags & 1:
                    continue
                current = replacements[index] if paired else None
                old_hash = binding_state.code_hash(original)
                new_hash = binding_state.code_hash(current) if current is not None else None
                if old_hash == new_hash:
                    continue
                mask = None
                if current is not None and len(original.co_code) < 8000:
                    mask = instruction_impact.offset_mask(original, current)
                    if (instruction_impact.metadata(original) == instruction_impact.metadata(current)
                            and len(originals) == 1):
                        # Calls reach this same function. The cold instruction
                        # comparison below accounts for its changed implementation.
                        canonical[module, name, current.co_firstlineno, new_hash] = old_hash
                if mask != 0:
                    sha = hashlib.blake2s(original.co_code, digest_size=12).digest()
                    details.append(((path, name, original.co_firstlineno, sha), -1 if mask is None else mask))
    return details, canonical


def inspect(edits, states, canonical, words, report, log):
    import unittest
    import query_state
    version, labels, names, uncertain, rows = profile()
    _, _, kinds = query_phases.profile()
    changed_files = tuple("/" + path for path in edits)
    opened = set()

    def watch(event, args):
        if event == "open" and isinstance(args[0], (str, bytes)):
            path = os.path.abspath(os.fsdecode(args[0]))
            if path.endswith(changed_files):
                opened.add(path)

    sys.addaudithook(watch)
    root = binding_state.ROOT / "cache/pinned"
    sys.path.insert(0, str(root))
    for path, source in edits.items():
        sys.meta_path.insert(0, replay_edit.Overlay(path, source, root))
    query_state.load_modules(names)
    finger = binding_state.Fingerprints(canonical)
    resolved, keys = {}, {}
    changed = uncertain
    reasons = []
    for desc, expected, mask in rows:
        if desc[0] == "*":
            different = desc[2] in words
        else:
            try:
                different = expected is None or query_state.projected(desc, finger, resolved, keys) != expected
            except errors:
                different = True
        if different:
            changed |= mask
            if log and len(reasons) < 12:
                reasons.append((desc[:3], mask.bit_count()))
    if log:
        print("query binding deltas", changed.bit_count(), reasons, file=sys.stderr, flush=True)
    if opened:
        return
    prepared = [(path, src, 0) for path, src in edits.items()]
    import row_programs
    row_programs.install(prepared)
    cases = query_replay.profile()[2]
    for kind, code_stages in states.items():
        stages = code_stages | (changed & kinds[kind])
        jobs = workload_replay.jobs_for_phases(kind, stages)
        if log:
            print("runtime query",kind,stages.bit_count(),None if jobs is None else len(jobs),
                  file=sys.stderr,flush=True)
        if jobs is None:
            continue
        try:
            for index, stages, rules in jobs:
                if not query_replay.verify_query(kind, cases[kind][index], stages, prepared, True, rules):
                    break
            else:
                if not opened:
                    report(kind)
        except errors + (AssertionError, RuntimeError):
            continue


def check(edits, states, canonical, words, budget):
    rd, wr = os.pipe()
    try:
        pid = os.fork()
    except OSError:
        os.close(rd)
        os.close(wr)
        raise
    if pid == 0:
        try:
            os.close(rd)
            signal.signal(signal.SIGPROF, signal.SIG_DFL)
            signal.signal(signal.SIGALRM, signal.SIG_DFL)
            signal.setitimer(signal.ITIMER_PROF, budget)
            signal.setitimer(signal.ITIMER_REAL, budget * 2 + 1)
            log = bool(os.environ.get("BR_QUERY_TRACE"))
            with open(os.devnull, "wb") as sink:
                os.dup2(sink.fileno(), 1)
                if not log:
                    os.dup2(sink.fileno(), 2)
            inspect(edits, states, canonical, words, lambda kind: os.write(wr, (kind+"\n").encode()), log)
        finally:
            os._exit(0)
    os.close(wr)
    with os.fdopen(rd, "rb") as result:
        output = result.read(128)
    os.waitpid(pid, 0)
    return set(output.decode().splitlines()) & states.keys()


def refine(selector, text, selected):
    global spent
    ids = {kind: selector.NUM[name] for kind, name in query_replay.IDS.items()
           if selector.NUM.get(name) in selected}
    if not ids or spent >= 9 or "sqlglot" in sys.modules:
        return selected
    gain = (selector.worker_cost(selector.split(selected))
            - selector.worker_cost(selector.split(selected-set(ids.values())))) / 1000
    if gain < 1.8:
        return selected
    start = workload_replay.cpu()
    try:
        prepared = workload_replay.prepare(selector, text)
        if not prepared:
            return selected
        edits, _, opaque, fixtures = prepared
        # Existing stage replay already handles function-only changes with a
        # fully matched executable footprint.
        if not opaque or fixtures:
            return selected
        patches = selector.parse_patch(text)
        protected = workload_replay.file_readers(selector, edits, patches)
        ids = {k:n for k,n in ids.items() if n not in protected}
        if not ids:
            return selected
        words = {word for patch in patches for h in patch["hunks"] for line in h["lines"]
                 if line.startswith(("+", "-")) for word in re.findall(r"\w+", line[1:])}
        description = describe(selector, edits, words)
        if description is None:
            return selected
        details, canonical = description
        version, labels = profile()[:2]
        stock, coverage, kinds = query_phases.profile()
        if tuple(version) != sys.version_info[:2] or labels != stock:
            return selected
        states = {}
        for kind in ids:
            stages = workload_replay.affected_stages(query_replay.IDS[kind], details, set(), coverage,
                                                     kinds[kind] | kinds["init"])
            if stages is not None and not stages & kinds["init"]:
                states[kind] = stages
        if not states:
            return selected
        budget = min(gain * 0.45, 3.5, 9 - spent - (workload_replay.cpu() - start))
        if budget < 0.20:
            return selected
        passed = check(edits, states, canonical, words, budget)
        if passed:
            print("runtime bindings verified queries", *sorted(passed), file=sys.stderr)
        return selected - {ids[k] for k in passed}
    except errors:
        return selected
    finally:
        spent += workload_replay.cpu() - start
