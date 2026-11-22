"""Join pristine observations of every component that can change a test's execution."""

from collections import defaultdict
from functools import lru_cache
import re
import sys

import argument_reads
import get_inputs
import instruction_impact
import lexical_inputs
import slot_inputs
import workload_replay


@lru_cache(1)
def observed_members():
    version, numbers, known, files = instruction_impact.observations()
    index = {}
    for path, members in files.items():
        groups = defaultdict(list)
        for (name, line, digest), visits in members.items():
            groups[name, line].append((digest, visits))
        index[path] = groups
    return version, numbers, known, index


def body_readers(path, edits, selected, known, observed):
    if path not in observed:
        return set(selected)
    mask = 0
    for (source, name, line, digest), changed in edits:
        if source != path:
            continue
        for actual, visits in observed[path].get((name, line), ()):
            for offsets, tests in visits.items():
                if digest != actual or changed & offsets:
                    mask |= tests
    return {n for n, bit in selected.items() if bit == 0 or bit & (mask | ~known)}


def value_readers(selector, piece, active):
    # Each of these checks proves that a changed stored value or lookup was
    # never observed. A test that merely passed on a partial patch is ineligible.
    matched = argument_reads.match(selector, piece)
    remaining = active if matched is None else active & matched
    for fn in (slot_inputs.refine, get_inputs.refine, lexical_inputs.refine):
        remaining = remaining & fn(selector, piece, remaining)
        if not remaining:
            break
    return remaining


@lru_cache(1)
def components(selector, text):
    starts = [m.start() for m in re.finditer(r"(?m)^diff --git ", text)]
    parts = {}
    for i, start in enumerate(starts):
        piece = text[start:starts[i+1] if i+1 < len(starts) else len(text)]
        files = selector.parse_patch(piece)
        if len(files) != 1 or not files[0]["old"] or files[0]["old"] != files[0]["new"]:
            return {}
        path = files[0]["old"]
        if path in parts:
            return {}
        parts[path] = piece
    return parts


def refine(selector, text, selected):
    if not selected or selector.worker_cost(selector.split(selected)) < 2550:
        return selected
    prepared = workload_replay.prepare(selector, text)
    if prepared is None:
        return selected
    edits, changes, opaque, fixtures = prepared
    version, numbers, known, observed = observed_members()
    if tuple(version) != sys.version_info[:2] or numbers.keys() != selector.NUM.keys():
        return selected
    patches = selector.parse_patch(text)
    possible = selected & workload_replay.file_readers(selector, set(edits) | fixtures, patches)
    pieces = components(selector, text)
    if not pieces or pieces.keys() != set(edits) | fixtures:
        return selected
    # An unknown fixture could be opened during collection or by a subprocess.
    # Until its attributed input reads are exhaustive, leave such combinations alone.
    if fixtures:
        return selected
    for path in edits:
        pending = selected - possible
        if not pending:
            break
        if path in opaque:
            possible |= value_readers(selector, pieces[path], pending)
        else:
            mapped = {n: numbers.get(selector.TEST_FILE_ID[n], 0) for n in pending}
            kept = body_readers(path, changes, mapped, known, observed)
            if kept:
                kept = value_readers(selector, pieces[path], kept)
            possible |= kept
    return possible
