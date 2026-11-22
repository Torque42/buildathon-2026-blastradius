"""Save the pristine parser's reference only if two fresh interpreter runs agree."""
import marshal
from pathlib import Path
import subprocess
import sys

root = Path(__file__).resolve().parents[1]
if sys.argv[1:] == ['capture']:
    sys.path.insert(0, str(root))
    import parser_frontier
    import shortlist
    reference = parser_frontier.fork_run(shortlist.PIN, {}, {'tokenizer','parser'}, 4.0, baseline=True)
    sys.stdout.buffer.write(marshal.dumps(reference))
else:
    command = [sys.executable, '-I', str(Path(__file__).resolve()), 'capture']
    first, second = [marshal.loads(subprocess.check_output(command, timeout=16)) for _ in range(2)]
    if first and first == second:
        (root/'cache/parser-reference').write_bytes(marshal.dumps((sys.version_info[:3], first)))
        print('Verified stable pristine parser references in two independent interpreters')
    else:
        print('Using a fresh parser reference at runtime')
