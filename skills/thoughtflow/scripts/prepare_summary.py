"""Return a stable, source-keyed input for the current host AI; no Codex CLI required."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.imports import MixedHistoryStore
from backend.organize import _input, source_key, INSTRUCTIONS, SCHEMA

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thread', required=True)
    args = parser.parse_args()
    try:
        conversation, reader = MixedHistoryStore().get(args.thread)
        _, nodes = reader.snapshot()
        payload, _ = _input(conversation, nodes)
        print(json.dumps({'conversationId': args.thread, 'sourceKey': source_key(nodes),
                          'instructions': INSTRUCTIONS, 'schema': SCHEMA, 'input': payload}, ensure_ascii=False))
    except Exception:
        print('Cannot prepare this conversation. Check its ID, sync status and input size (maximum 200,000 characters).', file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__': sys.exit(main())
