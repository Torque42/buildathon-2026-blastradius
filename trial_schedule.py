"""Buy a direct pytest observation only when its cost can change the worker schedule."""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from functools import lru_cache

ROOT = Path(__file__).resolve().parent
PIN = Path("/opt/sqlglot-eb11aec4be8cb2122afe713a4816662c3f0fb2fa")
SPENT = 0.0


def cost(selector, numbers):
    return selector.worker_cost(selector.split(numbers)) / 1000


def choose(selector, selected):
    ordered = sorted(selected, key=lambda n: (-selector.DUR[n], n))
    total = cost(selector, selected)
    # Closing a whole small selection saves the two-second worker fee. For a
    # large selection evaluate a group of the longest tests together, since
    # eliminating only one of several similar long tests can be worthless.
    candidates = []
    if len(ordered) <= 80 and sum(selector.DUR[n] for n in ordered) <= 2400:
        candidates.append(ordered)
    for step in (1, 2, 4, 8):
        if step <= len(ordered) and sum(selector.DUR[n] for n in ordered[:step]) <= 18000:
            candidates.append(ordered[:step])
    best = None
    for group in candidates:
        saved = total - cost(selector, selected - set(group))
        expected = 0.18 + sum(selector.DUR[n] for n in group)/1000
        if saved < 1.75 or saved < expected * 1.18:
            continue
        priority = saved - expected
        if best is None or priority > best[0]:
            best = priority, group, saved
    return best


@lru_cache(1)
def checkout():
    directory = tempfile.TemporaryDirectory(prefix="br-exact-")
    dest = Path(directory.name) / "src"
    shutil.copytree(PIN, dest, ignore=shutil.ignore_patterns("__pycache__", ".git", ".pytest_cache", "*.pyc"))
    return directory, dest


def edits_for(selector, text):
    patches = selector.parse_patch(text)
    if not 1 <= len(patches) <= 6:
        return
    edits = {}
    for patch in patches:
        path = patch["old"]
        if not path or path != patch["new"] or path in edits or Path(path).is_absolute() or ".." in Path(path).parts:
            return
        if not ((path.startswith("sqlglot/") and path.endswith(".py")) or
                (path.startswith("tests/fixtures/") and path.endswith(".sql"))):
            return
        target = PIN / path
        if not target.is_file() or target.is_symlink():
            return
        old = target.read_text(encoding="utf8")
        lines = old.split("\n")
        for h in patch["hunks"]:
            start = max(0, h["os"]-1)
            prior = [s[1:].rstrip("\r") for s in h["lines"] if s.startswith((" ", "-"))]
            if lines[start:start + len(prior)] != prior:
                return
        edits[path] = selector.apply_hunks(old, patch["hunks"])[0]
    return edits


def run_trial(selector, work, edits, budget):
    directory, tree = checkout()
    folder = Path(directory.name)
    request = folder/"request.json"
    response = folder/"response.json"
    response.unlink(missing_ok=True)
    nodes = {selector.TEST_FILE_ID[n]: n for n in work}
    request.write_text(json.dumps(list(nodes)))
    originals = {}
    env = dict(os.environ, HOME="/tmp", SKIP_INTEGRATION="0", PYTHONDONTWRITEBYTECODE="1",
               PYTHON_CPU_COUNT="2", OPENBLAS_NUM_THREADS="1", PYTHONPATH=str(tree))
    try:
        for name, source in edits.items():
            file = tree/name
            originals[file] = file.read_bytes()
            file.write_text(source, encoding="utf-8")
        proc = subprocess.Popen([sys.executable, "-B", str(ROOT/"trial_runner.py"), str(tree),
                                 str(request), str(response)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=tree, env=env, start_new_session=True)
        try:
            proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            pass
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        if proc.returncode == 0 and response.is_file():
            return {nodes[name] for name in json.loads(response.read_text()) if name in nodes}
    finally:
        for path, raw in originals.items():
            path.write_bytes(raw)
    return set()


def refine(selector, text, selected):
    global SPENT
    if not selected or SPENT >= 8 or not PIN.is_dir():
        return selected
    proposal = choose(selector, selected)
    if not proposal:
        return selected
    _, work, gain = proposal
    edits = edits_for(selector, text)
    if not edits:
        return selected
    start = time.monotonic()
    try:
        passed = run_trial(selector, work, edits, min(gain*0.55, 8-SPENT, 3.0))
    except (OSError, ValueError, KeyError):
        passed = set()
    SPENT += time.monotonic()-start
    print(f"direct-pytest {len(work)} requested, {len(passed)} passed, {time.monotonic()-start:.3f}s",
          file=sys.stderr)
    return selected - passed
