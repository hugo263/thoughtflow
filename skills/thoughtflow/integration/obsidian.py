"""Discover registered local vaults; write only a confirmed note inside one vault."""
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlencode

from integration.common import validate_tags
from integration.getnote import GetNoteError


def registry_path():
    if sys.platform == 'darwin':
        return Path.home() / 'Library/Application Support/obsidian/obsidian.json'
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'obsidian/obsidian.json'
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'obsidian/obsidian.json'


def _vaults():
    try:
        path = registry_path()
        if path.stat().st_size > 1024 * 1024:
            return {}
        values = json.loads(path.read_text(encoding='utf-8')).get('vaults', {})
    except (OSError, ValueError, AttributeError):
        return {}
    result = {}
    for ident, value in list(values.items())[:100] if isinstance(values, dict) else []:
        if not isinstance(ident, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', ident) or not isinstance(value, dict):
            continue
        location = value.get('path')
        if not isinstance(location, str) or not location:
            continue
        root = Path(location).expanduser().resolve()
        if root.is_dir() and (root / '.obsidian').is_dir() and os.access(root, os.R_OK | os.W_OK):
            result[ident] = {'root': root, 'name': root.name, 'open': bool(value.get('open'))}
    return result


def status():
    vaults = _vaults()
    targets = [{'id': ident, 'name': value['name']} for ident, value in vaults.items()]
    opened = [ident for ident, value in vaults.items() if value['open']]
    default = opened[0] if len(opened) == 1 else targets[0]['id'] if len(targets) == 1 else None
    return {'connected': bool(targets), 'available': bool(targets), 'destination': 'Obsidian',
            'targets': targets, 'defaultTarget': default, 'tagMode': 'Native tags property',
            'reason': '' if targets else 'No registered writable Obsidian vault found.'}


def save_note(title, content, request_id, tags=None, target=None):
    tags = validate_tags(tags)
    vault = _vaults().get(target)
    if vault is None:
        raise GetNoteError('invalid_target', 'Choose an available Obsidian vault.')
    root = vault['root']
    folder = root / 'Thought Flow'
    folder.mkdir(exist_ok=True)
    if folder.is_symlink() or folder.resolve().parent != root:
        raise GetNoteError('invalid_target', 'The Obsidian save directory is unavailable.')
    suffix = hashlib.sha256(request_id.encode('ascii')).hexdigest()[:24]
    slug = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '-', title).strip(' .')[:80] or 'Thought Flow'
    path = folder / (slug + '-' + suffix + '.md')
    document = ('---\ntitle: ' + json.dumps(title, ensure_ascii=False) + '\ntags: ' + json.dumps(tags, ensure_ascii=False)
                + '\nsource: "Codex Thought Flow"\n---\n\n' + content.strip() + '\n')
    # A persistent filename suffix preserves idempotency across service restarts.
    matches = list(folder.glob('*-' + suffix + '.md'))
    if matches:
        path = matches[0]
        if len(matches) != 1 or path.is_symlink() or path.read_text(encoding='utf-8') != document:
            raise GetNoteError('conflict', 'This request was used for other content. Reopen the preview.')
    else:
        try:
            with path.open('x', encoding='utf-8') as out:
                out.write(document)
        except FileExistsError:
            if path.is_symlink() or path.read_text(encoding='utf-8') != document:
                raise GetNoteError('conflict', 'Save request conflict. Reopen the preview.') from None
    return {'ok': True, 'id': suffix, 'url': 'obsidian://open?' + urlencode({'vault': vault['name'], 'file': 'Thought Flow/' + path.stem}),
            'destination': 'Obsidian · ' + vault['name'], 'tags': tags}
