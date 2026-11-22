"""Verify self-contained unittest cases when their potential schedule saving pays for replay."""
import ast
from functools import lru_cache
import importlib
import hashlib
import functools
import marshal
import os
from pathlib import Path
import signal
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent
PIN = ROOT / "cache/pinned"


@lru_cache(maxsize=1)
def catalog():
    return marshal.loads((ROOT / "cache/replayable-tests").read_bytes())


def changes(selector, text):
    patches = selector.parse_patch(text)
    if not 1 <= len(patches) <= 3:
        return None
    edits = {}
    for patch in patches:
        path = patch["old"]
        if not path or path != patch["new"] or path in edits or ".." in Path(path).parts:
            return None
        if path.startswith("sqlglot/") and path in selector.SRC:
            original = selector.SRC[path]
            new, removed, added = selector.apply(original, patch["hunks"])
            if len(removed) != 1 or len(added) != 1:
                return None
            line = removed[0] - 1
            before, after = original.splitlines(), new.splitlines()
            if len(before) != len(after) or before[:line] != after[:line] or before[line+1:] != after[line+1:]:
                return None
            if not selector.simple_token_change(before[line], after[line]):
                return None
        elif path.startswith("tests/fixtures/") and path.endswith(".sql") and (PIN/path).is_file():
            original = (PIN/path).read_text()
            new, _, _ = selector.apply(original, patch["hunks"])
        else:
            return None
        edits[path] = new
    return edits


def patched_tree(folder, edits):
    # All paths remain readable at their normal checkout locations, including
    # source inspection and fixtures opened relative to a test module's __file__.
    files = {Path(name): source for name, source in edits.items()}
    dirs = {parent for path in files for parent in path.parents}
    for relative in sorted(dirs, key=lambda p: len(p.parts)):
        destination = folder / relative
        destination.mkdir(exist_ok=True)
        for sibling in (PIN/relative).iterdir():
            child = relative/sibling.name
            if sibling.name != "__pycache__" and child not in files and child not in dirs:
                (folder/child).symlink_to(sibling, target_is_directory=sibling.is_dir())
    for path, source in files.items():
        dest = folder/path
        dest.write_text(source)
        # Imported source must differ from the stamp on any pristine cached bytecode.
        if path.suffix == ".py":
            os.utime(dest, (1, 1))


def witness():
    """Record which assertions in the actual pristine test suite execute."""
    state = [None, 0]
    def instrument(name, method):
        @functools.wraps(method)
        def called(self, *args, **kwargs):
            frame = sys._getframe(1)
            file = frame.f_code.co_filename.rpartition("/tests/")[2]
            if state[0] is not None and file != frame.f_code.co_filename:
                state[0].update(f"{file}:{frame.f_lineno}:{name}\n".encode())
                if name != "subTest":
                    state[1] += 1
            return method(self, *args, **kwargs)
        return called
    for name, method in list(vars(unittest.TestCase).items()):
        if callable(method) and (name.startswith("assert") or name == "subTest"):
            setattr(unittest.TestCase, name, instrument(name, method))
    return state


def check_test(node, state):
    filename, owner, method = node.split("::")
    module = importlib.import_module(filename[:-3].replace("/", "."))
    cls = getattr(module, owner)
    if not issubclass(cls, unittest.TestCase) or getattr(cls, "__unittest_skip__", False):
        return
    if any(k in vars(base) for base in cls.__mro__ if base is not unittest.TestCase
           for k in ("setUpClass", "tearDownClass")):
        return
    state[:] = [hashlib.blake2s(digest_size=16), 0]
    result = unittest.TestResult()
    unittest.TestSuite([cls(method)]).run(result)
    if (result.testsRun == 1 and result.wasSuccessful() and state[1] and
            not result.skipped and not result.expectedFailures):
        return state[0].digest()


def check(folder, work, pipe, budget, edits):
    signal.signal(signal.SIGPROF, signal.SIG_DFL)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.setitimer(signal.ITIMER_PROF, budget)
    signal.setitimer(signal.ITIMER_REAL, 2*budget + 0.8)
    os.chdir(folder)
    sys.path.insert(0, str(folder))
    sys.dont_write_bytecode = True
    os.environ["SKIP_INTEGRATION"] = "0"
    from replay_edit import Overlay
    for path, source in edits.items():
        if path.endswith(".py"):
            sys.meta_path.insert(0, Overlay(path, source, folder))
    state = witness()
    for number, node in work:
        try:
            if check_test(node, state) == catalog()[node]:
                os.write(pipe, f"{number}\n".encode())
        except Exception:
            pass


def replay(edits, work, budget):
    with tempfile.TemporaryDirectory(prefix="br-verification-") as tmp:
        folder = Path(tmp)
        patched_tree(folder, edits)
        reader, writer = os.pipe()
        try:
            pid = os.fork()
        except OSError:
            os.close(reader)
            os.close(writer)
            return set()
        if pid == 0:
            try:
                os.close(reader)
                with open(os.devnull, "wb") as sink:
                    os.dup2(sink.fileno(), 1)
                    os.dup2(sink.fileno(), 2)
                check(folder, work, writer, budget, edits)
            finally:
                os._exit(0)
        os.close(writer)
        try:
            with os.fdopen(reader, "rb") as output:
                passed = {int(line) for line in output}
        finally:
            os.waitpid(pid, 0)
        return passed


def refine(selector, text, selected):
    if not selected or len(selected) > 48 or "sqlglot" in sys.modules:
        return selected
    if sum(selector.DUR[n] for n in selected) > 1800:
        return selected
    valid = catalog()
    if any(selector.TEST_FILE_ID[n] not in valid for n in selected):
        return selected
    edits = changes(selector, text)
    if edits is None:
        return selected
    work = [(n, selector.TEST_FILE_ID[n]) for n in sorted(selected, key=lambda n: (selector.DUR[n], n))]
    gain = selector.worker_cost(selector.split(selected)) / 1000
    return selected - replay(edits, work, min(1.2, gain * 0.43))
