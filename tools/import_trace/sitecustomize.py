"""Record pinned files opened by a clean pytest process and its Python children."""
import importlib.util
import os
import sys

root = os.path.realpath(os.environ["BR_PIN"]) + os.sep
prefix = root + "sqlglot" + os.sep
record = os.open(os.environ["BR_IMPORT_LOG"], os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
seen = set()


def opened(event, args):
    if event != "open" or not isinstance(args[0], str):
        return
    path = args[0]
    if not os.path.isabs(path):
        path = os.path.abspath(path)
    if not path.startswith(prefix):
        return
    if path.endswith(".pyc"):
        try:
            path = importlib.util.source_from_cache(path)
        except ValueError:
            return
    if path.endswith(".py") and path not in seen:
        seen.add(path)
        os.write(record, (path[len(root):] + "\n").encode())


sys.addaudithook(opened)
