"""Explicit, user-authorized sync of public turns from other AI clients."""
from __future__ import annotations
import contextlib
import copy
import datetime as dt
import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from backend.history import HistoryStore, valid_thread_id
from backend.paths import runtime_dir

SOURCES = {'WorkBuddy', 'Kimi', 'Qoder', 'Claude'}
NAMESPACE = uuid.UUID('b7c3afc2-34dd-40c9-8e4f-541af7a36c54')

def _folder():
    path = runtime_dir() / 'imports'
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Import directory must not be a symlink')
    return path

def _checked(value, name, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()) or '\x00' in value:
        raise ValueError('Invalid ' + name)
    return value

@contextlib.contextmanager
def _lock(folder):
    fd = os.open(str(folder / '.sync.lock'), os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'a+b') as file:
        if os.name == 'nt':
            import msvcrt
            file.seek(0)
            if not file.read(1): file.write(b'0'); file.flush()
            file.seek(0); msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                file.seek(0); msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)

def _prepare_turn(data):
    if not isinstance(data, dict) or set(data) - {'turnId','question','answer','state','tools'}:
        raise ValueError('Invalid turn document')
    turn = _checked(data.get('turnId'), 'turnId', 128)
    if not re.fullmatch(r'[A-Za-z0-9._:-]+', turn): raise ValueError('Invalid turnId')
    question = _checked(data.get('question'), 'question', 16000)
    answer = _checked(data.get('answer', ''), 'answer', 200000, False)
    state = data.get('state')
    if state not in ('running', 'completed'): raise ValueError('Invalid state')
    tools = data.get('tools', [])
    if not isinstance(tools, list) or len(tools) > 100: raise ValueError('Invalid tools')
    clean_tools = []
    for index, item in enumerate(tools):
        if not isinstance(item, dict) or set(item) - {'label','status','detail'}: raise ValueError('Invalid tool')
        clean_tools.append({'id':turn + '-tool-' + str(index), 'label':_checked(item.get('label'), 'tool label', 200), 'status':_checked(item.get('status', 'completed'), 'tool status', 32), 'detail':_checked(item.get('detail', ''), 'tool detail', 8000, False)})
    return {'id':turn,'question':question,'answer':answer,'state':state,'tools':clean_tools}

def write_turn(data):
    if not isinstance(data, dict) or set(data) - {'source','session','turnId','title','question','answer','state','tools'}:
        raise ValueError('Invalid sync document')
    result = write_turns({k:data.get(k) for k in ('source','session','title')} | {'turns':[{k:v for k,v in data.items() if k not in {'source','session','title'}}]})
    return {'conversationId':result['conversationId'],'turnId':data['turnId'],'state':data['state']}

def write_turns(data):
    """Validate the whole batch and atomically update one conversation once."""
    if not isinstance(data, dict) or set(data) != {'source','session','title','turns'}:
        raise ValueError('Invalid batch document')
    source = data.get('source')
    if source not in SOURCES: raise ValueError('Unsupported source')
    session = _checked(data.get('session'), 'session', 200)
    title = _checked(data.get('title'), 'title', 200)
    incoming = data.get('turns')
    if not isinstance(incoming, list) or not 1 <= len(incoming) <= 2000: raise ValueError('Expected 1 to 2000 turns')
    prepared = [_prepare_turn(turn) for turn in incoming]
    if len({n['id'] for n in prepared}) != len(prepared): raise ValueError('Duplicate turnId in batch')
    ident = str(uuid.uuid5(NAMESPACE, source + '\n' + session))
    folder = _folder(); path = folder / (ident + '.json')
    with _lock(folder):
        if path.is_symlink(): raise ValueError('Invalid import path')
        if path.exists():
            if path.stat().st_size > 8 * 1024 * 1024: raise ValueError('Session too large')
            document = json.loads(path.read_text(encoding='utf-8'))
            if document.get('id') != ident or document.get('source') != source: raise ValueError('Invalid existing session')
        else:
            document = {'id':ident, 'source':source, 'title':title, 'nodes':[]}
        nodes = document['nodes']; by_id = {n['id']:n for n in nodes}
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for turn in prepared:
            existing = by_id.get(turn['id'])
            if existing and existing.get('state') == 'completed' and turn['state'] == 'running':
                raise ValueError('Cannot replace a completed turn with a running turn')
            if existing is None:
                if len(nodes) >= 2000: raise ValueError('Too many turns')
                if nodes and nodes[-1].get('state') == 'running': nodes[-1]['state'] = 'continued'
                existing = {'id':turn['id'], 'parentId':nodes[-1]['id'] if nodes else None, 'createdAt':now}
                nodes.append(existing); by_id[turn['id']] = existing
            existing.update(turn)
        document.update(title=title, updatedAt=now)
        serialized = json.dumps(document, ensure_ascii=False, separators=(',', ':'))
        if len(serialized.encode('utf-8')) > 8 * 1024 * 1024: raise ValueError('Session too large')
        fd, tmp = tempfile.mkstemp(prefix='.sync-', dir=str(folder))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as file:
                os.chmod(tmp, 0o600); file.write(serialized)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)
    return {'conversationId':ident,'syncedTurns':len(prepared),'totalTurns':len(nodes)}

class ImportedReader:
    def __init__(self, path): self.path, self.signature, self.revision, self.nodes = path, None, 0, []
    def sync(self):
        if self.path.is_symlink(): raise OSError('Invalid import path')
        stat = self.path.stat(); sig = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
        if sig != self.signature:
            if stat.st_size > 8 * 1024 * 1024: raise OSError('Session too large')
            doc = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(doc.get('nodes'), list): raise OSError('Invalid session')
            self.nodes = doc['nodes']; self.signature = sig; self.revision += 1
        return self.revision
    def snapshot(self):
        self.sync()
        return self.revision, copy.deepcopy(self.nodes)

class MixedHistoryStore:
    def __init__(self): self.codex, self.readers = HistoryStore(), {}
    def _meta(self, path):
        if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024: raise ValueError('Invalid session')
        doc = json.loads(path.read_text(encoding='utf-8'))
        if doc.get('source') not in SOURCES or doc.get('id') != path.stem or not valid_thread_id(path.stem): raise ValueError('Invalid session')
        return {'id':doc['id'], 'title':doc['title'], 'source':doc['source'], 'updatedAt':doc['updatedAt'], 'archived':False, 'compressed':False, 'available':True}
    def refresh(self):
        rows = self.codex.refresh()
        for path in _folder().glob('*.json'):
            try: rows.append(self._meta(path))
            except (OSError, ValueError, KeyError, TypeError): continue
        return sorted(rows, key=lambda x:x.get('updatedAt', ''), reverse=True)
    def get(self, ident):
        if not valid_thread_id(ident): raise KeyError('Invalid conversation')
        path = _folder() / (ident.lower() + '.json')
        if path.exists() and not path.is_symlink():
            meta = self._meta(path)
            reader = self.readers.setdefault(ident.lower(), ImportedReader(path))
            return meta, reader
        return self.codex.get(ident)
    def rebind(self, ident, reader, *, force=False):
        if isinstance(reader, ImportedReader):
            return self.get(ident)[1]
        return self.codex.rebind(ident, reader, force=force)
