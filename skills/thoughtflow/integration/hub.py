"""Known destination adapters. Detection reuses existing authorization only."""
import hashlib
import os
import threading
import time
from pathlib import Path

from integration import getnote, notion, obsidian
from integration.common import validate_tags

_lock = threading.Lock()
_cache = {}


def _signature():
    path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
    try:
        stat = path.stat()
        identity = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        identity = (str(path), None)
    # Fingerprints only. Credentials never enter responses or persistent state.
    tokens = hashlib.sha256('\0'.join(os.environ.get(name, '') for name in notion._TOKEN_NAMES).encode()).hexdigest()
    return identity, tokens


def status():
    signature = _signature()
    with _lock:
        current = {}
        for name, check in (('getnote', getnote.status), ('notion', notion.status)):
            cached = _cache.get(name)
            if cached and cached[0] == signature and time.monotonic() < cached[1]:
                current[name] = dict(cached[2])
                continue
            try:
                result = check(refresh=True) if name == 'getnote' else check()
            except Exception:
                result = {'connected': False, 'available': False, 'reason': 'Cannot verify the connection. Try again later.'}
            _cache[name] = (signature, time.monotonic() + (300 if result.get('connected') else 30), result)
            current[name] = dict(result)
    current['obsidian'] = obsidian.status()
    current['getnote'].setdefault('tagMode', 'Native note tags')
    return current


def save_note(provider, title, content, request_id, tags=None, target=None):
    tags = validate_tags(tags)
    if provider == 'getnote':
        return getnote.save_note(title, content, request_id, tags=tags) if tags else getnote.save_note(title, content, request_id)
    if provider == 'obsidian':
        return obsidian.save_note(title, content, request_id, tags=tags, target=target)
    if provider == 'notion':
        return notion.save_note(title, content, request_id, tags=tags, target=target)
    raise getnote.GetNoteError('invalid_provider', 'Invalid save destination.')
