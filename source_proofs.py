"""Build-time indices for source locations on the immutable SQLGlot pin."""
from functools import lru_cache
from pathlib import Path
import pickle

ROOT = Path(__file__).resolve().parent / "cache" / "source-proofs"


@lru_cache(maxsize=12)
def read(path):
    record = ROOT / (path + ".pickle")
    if record.is_file():
        return pickle.loads(record.read_bytes())
    return None
