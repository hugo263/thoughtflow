"""Save a host-generated structured summary after validating its source references."""
import datetime as dt
import hashlib
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backend.imports import MixedHistoryStore
from backend.organize import _input, source_key, validate_result, OrganizeError
from backend.paths import runtime_dir

def main():
    try:
        raw = sys.stdin.read(1024*1024+1)
        if len(raw) > 1024*1024: raise ValueError('Input too large')
        data = json.loads(raw)
        if not isinstance(data, dict) or set(data) - {'conversationId','result','sourceKey'} or not {'conversationId','result'} <= set(data): raise ValueError('Invalid document')
        ident = data['conversationId']
        conversation, reader = MixedHistoryStore().get(ident)
        _, nodes = reader.snapshot()
        if 'sourceKey' in data and data['sourceKey'] != source_key(nodes):
            raise ValueError('Conversation changed. Prepare a fresh summary input before publishing.')
        payload, encoded = _input(conversation, nodes)
        result = validate_result(data['result'], payload)
        job = {'id':uuid.uuid4().hex, 'conversationId':ident, 'sourceKey':source_key(nodes), 'inputDigest':hashlib.sha256(encoded.encode('utf-8')).hexdigest(), 'status':'completed', 'turnCount':len(nodes), 'sourceTurnIds':[node['id'] for node in nodes], 'createdAt':dt.datetime.now(dt.timezone.utc).isoformat(), 'engine':'host-ai', **result}
        folder = runtime_dir() / 'organized'; folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = folder / (ident + '.json')
        if path.is_symlink(): raise ValueError('Invalid summary path')
        fd, tmp = tempfile.mkstemp(prefix='.summary-', dir=str(folder))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                os.chmod(tmp,0o600); json.dump(job,file,ensure_ascii=False,separators=(',',':'))
            os.replace(tmp,path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
        print(json.dumps({'conversationId':ident,'status':'completed'},ensure_ascii=False))
    except (ValueError, TypeError, OSError, KeyError, OrganizeError) as exc:
        print('Summary failed: ' + str(exc), file=sys.stderr); return 1
    return 0
if __name__ == '__main__': sys.exit(main())
