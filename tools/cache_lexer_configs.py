"""Save the unmodified derived configurations corresponding to the measured tokenizer reads."""
import marshal
from pathlib import Path
import sys

root=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(root),str(root/"cache/pinned")]
import lexer_contracts
tables,indirect=lexer_contracts.take_snapshot()
(root/"cache/lexer-configs").write_bytes(marshal.dumps((tables,indirect)))
print("Verified",len(tables),"concrete tokenizer classes")
