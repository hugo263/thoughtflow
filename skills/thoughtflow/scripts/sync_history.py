"""Import authorized visible history in one atomic batch; no model call."""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.imports import write_turns

def main():
    try:
        raw = sys.stdin.read(8 * 1024 * 1024 + 1)
        if len(raw) > 8 * 1024 * 1024: raise ValueError('Input too large')
        print(json.dumps(write_turns(json.loads(raw)), ensure_ascii=False))
    except (ValueError, TypeError, OSError, KeyError) as exc:
        print('Batch sync failed: ' + str(exc), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__': sys.exit(main())
