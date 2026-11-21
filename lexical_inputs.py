"""Identify tests whose pristine lexical lookup can distinguish a table change."""
import ast
from collections import defaultdict
from functools import lru_cache
import gzip
import marshal
from pathlib import Path
import re

from replay_edit import preserve_scope

WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*\Z")


@lru_cache(maxsize=1)
def reads():
    return marshal.loads(gzip.decompress((Path(__file__).parent / "maps/lexical-reads.gz").read_bytes()))


def class_tables(tree):
    tables = {}
    def visit(body, owners=()):
        for statement in body:
            if isinstance(statement, ast.ClassDef):
                visit(statement.body, (*owners, statement.name))
            if not owners:
                continue
            if isinstance(statement, ast.AnnAssign):
                name = statement.target
            elif isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                name = statement.targets[0]
            else:
                continue
            if isinstance(name, ast.Name) and name.id in ("KEYWORDS", "RESERVED_KEYWORDS"):
                tables[(".".join(owners), name.id)] = statement
    visit(tree.body)
    return tables


@lru_cache(maxsize=32)
def pinned_tables(selector, path):
    return class_tables(selector.source_tree(path))


@lru_cache(maxsize=1)
def token_types(selector):
    tree = selector.source_tree("sqlglot/tokenizer_core.py")
    cls = next(s for s in tree.body if isinstance(s, ast.ClassDef) and s.name == "TokenType")
    return {t.id for s in cls.body if isinstance(s, ast.Assign) for t in s.targets if isinstance(t, ast.Name)}


@lru_cache(maxsize=1)
def protected_words(selector):
    words = {"(+)"}
    for path, source in selector.SRC.items():
        if not path.startswith("sqlglot/") or not ("KEYWORDS.pop(" in source or "HINT_START" in source):
            continue
        for node in ast.walk(selector.source_tree(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "pop" and node.args:
                receiver = node.func.value
                if getattr(receiver, "id", getattr(receiver, "attr", None)) == "KEYWORDS" and isinstance(node.args[0], ast.Constant):
                    words.add(node.args[0].value)
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Constant):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(isinstance(t, ast.Name) and t.id == "HINT_START" for t in targets):
                    words.add(node.value.value)
    return words


def dictionary(selector, tree):
    if not isinstance(tree, ast.Dict):
        return None
    spreads, entries = [], defaultdict(list)
    for key, value in zip(tree.keys, tree.values):
        if key is None:
            spreads.append(ast.dump(value))
        elif isinstance(key, ast.Constant) and isinstance(key.value, str):
            entries[key.value].append((len(spreads), ast.dump(value), isinstance(value, ast.Attribute)
                                       and isinstance(value.value, ast.Name) and value.value.id == "TokenType"
                                       and value.attr in token_types(selector)))
        else:
            # A pinned computed key is an ordering boundary just like a spread.
            # The expression and its value must survive the edit byte-for-byte in the AST.
            spreads.append((ast.dump(key), ast.dump(value)))
    return spreads, entries


def changed_words(selector, name, old, new):
    if name == "RESERVED_KEYWORDS":
        if not all(isinstance(n, ast.Set) and all(isinstance(v, ast.Constant) and type(v.value) is str for v in n.elts) for n in (old, new)):
            return None
        return {v.value for v in old.elts} ^ {v.value for v in new.elts}
    first, second = dictionary(selector, old), dictionary(selector, new)
    if first is None or second is None or first[0] != second[0]:
        return None
    a, b = first[1], second[1]
    delta = {k for k in a.keys() | b.keys() if a.get(k) != b.get(k)}
    if not all(valid for k in delta for _, _, valid in a.get(k, []) + b.get(k, [])):
        return None
    return delta


def refine(selector, text, selected):
    if not selected or "KEYWORDS" not in text and not re.search(r"(?m)^[-+]\s*['\"]", text):
        return selected
    patches = selector.parse_patch(text)
    if len(patches) != 1:
        return selected
    patch = patches[0]
    path = patch["old"]
    if not path or path != patch["new"] or path not in selector.SRC or not (
            path == "sqlglot/tokens.py" or path.startswith(("sqlglot/dialects/", "sqlglot/generators/"))):
        return selected
    if path in selector.profile()["source_reads"] or not patch["hunks"]:
        return selected
    old_tables = pinned_tables(selector, path)
    if not old_tables:
        return selected
    source = selector.SRC[path]
    updated, removed, added = selector.apply(source, patch["hunks"])
    lines = set(removed) | set(added)
    if not lines or len(lines) > 40:
        return selected
    for key, stmt in old_tables.items():
        if not (stmt.lineno < min(lines) and max(lines) <= stmt.end_lineno):
            continue
        parsed = ast.parse(updated)
        after = class_tables(parsed).get(key)
        if after is None or stmt.lineno != after.lineno:
            return selected
        # Verify the complete program except the table expression. This also
        # covers diffs that insert another statement inside the same line span.
        name = key[1]
        words = changed_words(selector, name, stmt.value, after.value)
        if words is None or not all(WORD.fullmatch(word) for word in words):
            return selected
        if name == "KEYWORDS" and words & protected_words(selector):
            # A removed word can make a derived dialect's unguarded pop fail during collection.
            return set(selector.ALL)
        after.value = stmt.value
        if ast.dump(parsed) != ast.dump(selector.source_tree(path)):
            return selected
        catalog, contexts, sites = reads()
        if set(selector.NUM) != set(catalog):
            return selected
        site = "token" if name == "KEYWORDS" else path[:-3].replace("/", ".") + ":" + key[0]
        if site not in sites:
            return selected
        bits = sites[site].get("*", 0)
        for word in words:
            bits |= sites[site].get(word, 0)
        keep = selector.tests_of(selector.M["opens"].get(path)) | selector.UNTRACED_POOL_TESTS
        keep.update(n for n, (raw, _) in selector.EXTRA.items() if path in raw)
        last = stmt.end_lineno if len(removed) == len(added) else len(source.splitlines())
        mask = ((1 << (last - stmt.lineno + 1)) - 1) << stmt.lineno
        keep.update(preserve_scope(selector, path, mask, selector.ALL))
        while bits:
            bit = bits & -bits
            bits -= bit
            context = contexts[bit.bit_length() - 1]
            if context in selector.NUM:
                keep.add(selector.NUM[context])
            elif context.startswith("scope:") and context[6:] in selector.BY_FILE:
                keep.update(selector.BY_FILE[context[6:]])
            else:
                return set(selector.ALL)
        return keep
    return selected
