"""Replay the parse and serialization performed before dispatching one fixture query."""
import json
import pickle
import sys
from pathlib import Path

def main():
    root=Path(__file__).resolve().parent
    sys.path[:0]=[str(root),str(root/"cache/pinned")]
    from sqlglot import parse_one,transpile
    from replay_state import digest
    for old,new,executed in json.load(sys.stdin):
        fresh=pickle.loads(pickle.dumps(parse_one(new)))
        if executed:
            original=pickle.loads(pickle.dumps(parse_one(old)))
            if digest(fresh,())!=digest(original,()) or transpile(old,write="duckdb")[0]!=transpile(new,write="duckdb")[0]:
                return
    print("matched")
if __name__=="__main__":
    main()
