"""Spend verification CPU first on patch/test groups that can release a worker."""

from functools import lru_cache
import json
from pathlib import Path
import sys

import shortlist
import workload_replay

ROOT = Path(__file__).resolve().parent


@lru_cache(1)
def timings():
    return json.loads((ROOT / "maps/pytest-costs.json").read_bytes())


def patched_files(selector, text):
    edits = {}
    for form in selector.parse_patch(text):
        name = form["old"]
        if (not name or name != form["new"] or name in edits or not form["hunks"]
                or Path(name).is_absolute() or ".." in Path(name).parts):
            return
        if name in selector.SRC and name.startswith("sqlglot/") and name.endswith(".py"):
            original = selector.SRC[name]
        elif name.startswith("tests/fixtures/") and (shortlist.PIN / name).is_file():
            original = (shortlist.PIN / name).read_text()
        else:
            return
        rows = original.splitlines()
        for h in form["hunks"]:
            old = [t[1:].rstrip("\r") for t in h["lines"] if t.startswith((" ", "-"))]
            i = max(0, h["os"] - 1)
            if rows[i:i+len(old)] != old:
                return
        edits[name] = selector.apply(original, form["hunks"])[0]
    return edits


def plan(selector, selected, attempted):
    witness = shortlist.catalog()
    measured = timings()
    eligible = [n for n in selected - attempted if selector.TEST_FILE_ID[n] in witness]
    if not eligible:
        return
    cost = selector.worker_cost(selector.split(selected)) / 1000
    # The full-passing outcome is an upper bound on the benefit of verification.
    if cost - selector.worker_cost(selector.split(selected - set(eligible))) / 1000 < 0.5:
        return
    def runtime(n):
        return max(0.0008, measured.get(selector.TEST_FILE_ID[n], selector.DUR[n]/1000))
    ordered = sorted(eligible, key=lambda n: (-selector.DUR[n], n))
    by_ratio = sorted(ordered, key=lambda n: -selector.DUR[n]/runtime(n))
    options, seen = [], set()
    for order in (ordered, by_ratio):
        for size in (1, 2, 4, 8, 16, 32, 64, 128, len(order)):
            group = frozenset(order[:size])
            if group in seen:
                continue
            seen.add(group)
            files = {selector.TEST_FILE[n] for n in group}
            estimate = 0.085 + 0.006*len(files) + sum(runtime(n) for n in group)
            if estimate > 1.85:
                continue
            gain = cost - selector.worker_cost(selector.split(selected - group)) / 1000
            if gain < 0.5 or gain < 1.8 * estimate:
                continue
            options.append((gain / estimate, gain, estimate, sorted(group, key=lambda n: (-selector.DUR[n], n))))
    if options:
        return max(options, key=lambda row: row[1] - 1.35*row[2])


def refine(selector, entries):
    if "sqlglot" in sys.modules:
        return
    ranked = []
    for entry in entries:
        text, selected, eligible, tried = entry[1], entry[2], entry[4], entry[5]
        if eligible and selected:
            proposal = plan(selector, selected, tried)
            if proposal:
                ranked.append((proposal, entry))
    start = workload_replay.cpu()
    for (ratio, gain, estimate, group), entry in sorted(ranked, key=lambda r: -r[0][0]):
        remaining = 5.0 - (workload_replay.cpu() - start)
        if remaining < 0.15:
            break
        try:
            edits = patched_files(selector, entry[1])
            if not edits:
                continue
            work = [(n, selector.TEST_FILE_ID[n]) for n in group]
            budget = min(remaining, 2.6, 0.58 * gain, 2.1 * estimate + 0.14)
            passed = shortlist.replay(edits, work, budget)
            if passed:
                entry[2] = entry[2] - passed
            print(f"batch assertions {len(passed)}/{len(group)} on {entry[0].stem}", file=sys.stderr)
        except (OSError, ValueError, TypeError, SyntaxError, KeyError, RecursionError):
            continue
