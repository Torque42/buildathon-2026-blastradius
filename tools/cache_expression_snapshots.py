"""Create fast native pickle snapshots, and verify each reference against the existing observation."""

import copyreg
import io
import marshal
from pathlib import Path
import pickle
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "cache/pinned")]
from sqlglot import exp
import query_replay
import snapshot_cache as fast
from replay_state import digest


def expression_state(value):
    # The ordinary SQLGlot pickle reducer re-parses its serialized node list.
    # This stores the already reconstructed slot graph, including parent links.
    state = object.__getstate__(value)
    if isinstance(state, tuple) and "_hash" in state[1]:
        state[1]["_hash"] = None
    return copyreg.__newobj__, (type(value),), state


class NativePickler(pickle.Pickler):
    def reducer_override(self, obj):
        if isinstance(obj, exp.Expr):
            return expression_state(obj)
        return NotImplemented


def pack(value):
    out = io.BytesIO()
    NativePickler(out, protocol=5).dump(value)
    return out.getvalue()


def main():
    destination = fast.CACHE
    destination.mkdir(parents=True, exist_ok=True)
    hashes = {}
    snapshot_count = 0
    for folder, has_rule in (("query-inputs", False), ("rules", True)):
        for f in (ROOT / "cache" / folder).iterdir():
            original = pickle.loads(f.read_bytes())
            try:
                packed = pack(original)
                actual = pickle.loads(packed)
                tree = original[2] if has_rule else original
                restored = actual[2] if has_rule else actual
                if (digest(tree, ()) != digest(restored, ())
                        or has_rule and (actual[:2] != original[:2] or actual[3].keys() != original[3].keys())):
                    raise ValueError("Expression snapshot changed the pristine graph")
                # Preserve kwargs such as MappingSchema using their original
                # pickle behavior, including nested expressions.
                if has_rule and pickle.dumps(original[3],protocol=5) != pickle.dumps(actual[3],protocol=5):
                    continue
                (destination / (folder+"-"+f.name)).write_bytes(packed)
                snapshot_count += 1
            except (AttributeError, TypeError, ValueError, pickle.PickleError, RecursionError):
                continue
    index = query_replay.rule_index()
    def remember(tree, expected):
        if digest(tree, ()) == expected:
            try:
                hashes[expected] = fast.quick_hash(tree)
            except (TypeError, ValueError, RecursionError):
                pass
    for cases in query_replay.profile()[2].values():
        for case in cases:
            if not case["execute"]:
                continue
            final = query_replay.restore(case["optimized_input"])
            remember(final, case["optimized"])
            remember(query_replay.restore(case["parse_input"]),case["parsed"])
            rules = case.get("rules", ())
            for i, (_, expected, _) in enumerate(rules):
                if i+1 == len(rules):
                    following = final
                else:
                    start, _, j = index[rules[i+1][0]]
                    following = pickle.loads((ROOT / "cache/rules" / f"{start}-{j}").read_bytes())[2]
                remember(following, expected)
    (destination / "hashes").write_bytes(marshal.dumps(hashes))
    print("Verified",snapshot_count,"expression snapshots and",len(hashes),"pristine output hashes")


if __name__ == "__main__":
    main()
