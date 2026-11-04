"""Reuse existing Notion API credentials or a compatible configured stdio MCP.

No OAuth credentials are extracted from other applications. No remote content is
created during discovery. All destination selection happens in the save preview.
"""
import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler

from backend.paths import runtime_dir
from integration import getnote
from integration.common import validate_tags
from integration.getnote import GetNoteError

LEDGER = runtime_dir() / 'notion-saves.json'
_ledger_lock = threading.Lock()
_TOKEN_NAMES = ('NOTION_TOKEN', 'NOTION_API_KEY', 'NOTION_INTEGRATION_TOKEN')


def _configuration():
    path = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex') / 'config.toml'
    values = []
    try:
        if getnote.tomllib:
            with path.open('rb') as source:
                servers = getnote.tomllib.load(source).get('mcp_servers', {})
            values = [value for name, value in servers.items() if 'notion' in name.lower() and isinstance(value, dict)]
        else:
            source = path.read_text(encoding='utf-8')
            names = re.findall(r'^\s*\[mcp_servers\.([\w-]*notion[\w-]*)\]\s*$', source, re.M | re.I)
            values = [getnote._parse_fallback(source, name) for name in names[:10]]
    except (OSError, ValueError):
        pass
    if values and all(value.get('enabled', True) is False for value in values):
        raise GetNoteError('disabled', 'The local Notion connection is disabled. Enable it in its original configuration.')
    for name in _TOKEN_NAMES:
        if os.environ.get(name):
            return {'token': os.environ[name]}
    for value in values:
        if value.get('enabled', True) is False:
            continue
        env = value.get('env', {})
        if not isinstance(env, dict):
            continue
        for name in _TOKEN_NAMES:
            if isinstance(env.get(name), str) and env[name]:
                return {'token': env[name]}
        try:
            headers = json.loads(env.get('OPENAPI_MCP_HEADERS', '{}'))
            authorization = headers.get('Authorization', headers.get('authorization', ''))
            if isinstance(authorization, str) and authorization.startswith('Bearer '):
                return {'token': authorization[7:]}
        except (ValueError, AttributeError, TypeError):
            pass
        command, args = value.get('command'), value.get('args', [])
        if (isinstance(command, str) and shutil.which(command) and isinstance(args, list)
                and all(isinstance(arg, str) for arg in args)
                and all(isinstance(key, str) and isinstance(item, str) for key, item in env.items())):
            return {'mcp': {'command': command, 'args': args, 'env': env, 'cwd': value.get('cwd')}}
    raise GetNoteError('not_configured', 'No reusable Notion API credentials or compatible local MCP found. Signing in to another app does not grant this program access.')


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *unused):
        return None


class Session:
    def __init__(self):
        self.config = _configuration()
        self.mcp = None
        self.names = set()

    def __enter__(self):
        if 'mcp' in self.config:
            self.mcp = getnote._MCP(self.config['mcp'])
            try:
                self.mcp.initialize()
                tools = self.mcp.request('tools/list', {}).get('tools', [])
                self.names = {tool.get('name') for tool in tools if isinstance(tool, dict)}
                if not {'API-post-page', 'API-post-search'}.issubset(self.names):
                    raise GetNoteError('unsupported_tool', 'This Notion MCP save protocol is unsupported. Use the standard Notion API or a compatible OpenAPI MCP.')
            except Exception:
                self.mcp.close()
                raise
        return self

    def __exit__(self, *unused):
        if self.mcp:
            self.mcp.close()

    def request(self, method, path, body=None):
        if self.mcp:
            if path == 'search':
                tool, arguments = 'API-post-search', body or {}
            elif path == 'pages' and method == 'POST':
                tool, arguments = 'API-post-page', body
            else:
                kind, ident = path.split('/', 1)
                candidates = {'pages': ['API-retrieve-a-page', 'API-get-page'],
                              'data_sources': ['API-retrieve-a-data-source'],
                              'databases': ['API-retrieve-a-database']}[kind]
                tool = next((name for name in candidates if name in self.names), None)
                if not tool:
                    raise GetNoteError('unsupported_tool', 'This Notion MCP cannot read destination properties.')
                arguments = {kind.rstrip('s') + '_id': ident}
            return self.mcp.tool(tool, arguments)
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None
        request = Request('https://api.notion.com/v1/' + path, data=payload, method=method,
                          headers={'Authorization': 'Bearer ' + self.config['token'], 'Notion-Version': '2025-09-03',
                                   'Content-Type': 'application/json'})
        try:
            with build_opener(_NoRedirect()).open(request, timeout=15) as response:
                data = response.read(2 * 1024 * 1024 + 1)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError
            result = json.loads(data)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except HTTPError as error:
            code = 'notion_rejected' if 400 <= error.code < 500 else 'unknown_result'
            raise GetNoteError(code, 'Notion rejected the request. Check the connection, page permissions and properties.' if code == 'notion_rejected'
                               else 'Notion has not confirmed the result. Check Notion first to avoid duplicates.') from None
        except (OSError, URLError, ValueError):
            raise GetNoteError('unknown_result', 'Notion disconnected or returned an unexpected result. Check Notion before retrying the save.') from None


def status():
    try:
        with Session() as session:
            # Search verifies existing authorization without creating or changing data.
            response = session.request('POST', 'search', {'page_size': 1})
            if not isinstance(response.get('results'), list):
                raise GetNoteError('protocol_error', 'Cannot verify the Notion connection.')
        return {'connected': True, 'available': True, 'destination': 'Notion', 'requiresTarget': True,
                'tagMode': 'Data sources use existing tag properties; page tags are saved in the body.'}
    except GetNoteError as error:
        return {'connected': False, 'available': error.code not in {'not_configured', 'disabled'}, 'reason': error.public_message}


def _title(item):
    values = item.get('title', [])
    if not values:
        values = next((value.get('title', []) for value in item.get('properties', {}).values()
                       if isinstance(value, dict) and value.get('type') == 'title'), [])
    return ''.join(value.get('plain_text', value.get('text', {}).get('content', '')) for value in values if isinstance(value, dict))[:200] or 'Untitled'


def destinations(query=''):
    with Session() as session:
        response = session.request('POST', 'search', {'page_size': 100, **({'query': query[:100]} if query else {})})
    targets = []
    for item in response.get('results', []):
        kind = item.get('object') if isinstance(item, dict) else None
        if kind not in {'page', 'data_source', 'database'} or item.get('archived') or item.get('in_trash'):
            continue
        try:
            ident = str(uuid.UUID(item['id']))
        except (ValueError, TypeError, KeyError):
            continue
        # Current database containers expose data sources; rows belong to those.
        if kind == 'database' and item.get('data_sources'):
            for source in item['data_sources'][:10]:
                try:
                    source_id = str(uuid.UUID(source['id']))
                    targets.append({'id': 'data_source_' + source_id, 'name': _title(item) + ' · ' + str(source.get('name') or 'Data source')[:100], 'kind': 'data_source'})
                except (ValueError, TypeError, KeyError):
                    continue
        else:
            targets.append({'id': kind + '_' + ident, 'name': _title(item), 'kind': kind})
    return {'targets': targets, 'partial': bool(response.get('has_more'))}


def _chunks(value, limit=1800):
    part, length = '', 0
    for char in value:
        size = 2 if ord(char) > 0xffff else 1
        if length + size > limit:
            yield part
            part, length = '', 0
        part += char
        length += size
    if part:
        yield part


def _blocks(content, tags):
    result = []
    if tags:
        result.append({'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': 'Tags: ' + ' '.join('#' + tag for tag in tags)}}]}})
    fenced, code = False, []
    for line in content.splitlines():
        if line.strip().startswith('```'):
            if fenced:
                for part in _chunks('\n'.join(code)):
                    result.append({'object': 'block', 'type': 'code', 'code': {'language': 'plain text', 'rich_text': [{'type': 'text', 'text': {'content': part}}]}})
                code = []
            fenced = not fenced
            continue
        if fenced:
            code.append(line)
            continue
        if not line.strip():
            continue
        match = re.match(r'^(#{1,3})\s+(.+)', line)
        kind, value = ('heading_' + str(len(match[1])), match[2]) if match else ('paragraph', line)
        if not match and re.match(r'^\s*[-*+]\s+', line):
            kind, value = 'bulleted_list_item', re.sub(r'^\s*[-*+]\s+', '', line)
        elif not match and re.match(r'^\s*\d+[.)]\s+', line):
            kind, value = 'numbered_list_item', re.sub(r'^\s*\d+[.)]\s+', '', line)
        for part in _chunks(value):
            result.append({'object': 'block', 'type': kind, kind: {'rich_text': [{'type': 'text', 'text': {'content': part}}]}})
    if code:
        for part in _chunks('\n'.join(code)):
            result.append({'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': [{'type': 'text', 'text': {'content': part}}]}})
    if len(result) > 100:
        raise GetNoteError('invalid_input', 'Notion saves support up to 100 blocks. Shorten this card before saving.')
    return result


def _payload(session, title, content, tags, target):
    kind = next((kind for kind in ('data_source', 'database', 'page') if isinstance(target, str) and target.startswith(kind + '_')), None)
    try:
        ident = str(uuid.UUID(target[len(kind) + 1:])) if kind else None
    except (ValueError, TypeError):
        ident = None
    if not ident:
        raise GetNoteError('invalid_target', 'Choose an accessible Notion page or data source.')
    metadata = session.request('GET', {'page': 'pages', 'database': 'databases', 'data_source': 'data_sources'}[kind] + '/' + ident)
    if metadata.get('archived') or metadata.get('in_trash'):
        raise GetNoteError('invalid_target', 'The selected Notion destination is archived.')
    schema = metadata.get('properties', {})
    title_name = 'title' if kind == 'page' else next((name for name, value in schema.items() if value.get('type') == 'title'), None)
    if title_name is None:
        raise GetNoteError('invalid_target', 'The selected Notion data source has no writable title property.')
    properties = {title_name: {'title': [{'type': 'text', 'text': {'content': title}}]}}
    tag_columns = [name for name, value in schema.items() if value.get('type') == 'multi_select'] if kind != 'page' else []
    tag_name = next((name for name in tag_columns if name.lower() in {'tags', 'tag', '标签'}), tag_columns[0] if len(tag_columns) == 1 else None)
    if tag_name and tags:
        properties[tag_name] = {'multi_select': [{'name': tag} for tag in tags]}
    return {'parent': {kind + '_id': ident}, 'properties': properties, 'children': _blocks(content, tags)}


def _store_ledger(data):
    LEDGER.parent.mkdir(exist_ok=True, parents=True)
    temporary = LEDGER.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    os.replace(str(temporary), str(LEDGER))


def save_note(title, content, request_id, tags=None, target=None):
    tags = validate_tags(tags)
    digest = hashlib.sha256(json.dumps([title, content, tags, target], ensure_ascii=False).encode('utf-8')).hexdigest()
    with _ledger_lock:
        try:
            ledger = json.loads(LEDGER.read_text(encoding='utf-8')) if LEDGER.exists() else {}
            if not isinstance(ledger, dict):
                raise ValueError
        except (OSError, ValueError):
            raise GetNoteError('receipt_error', 'Cannot read the Notion save receipt. Creation stopped to avoid duplicates.') from None
        previous = ledger.get(request_id)
        if previous:
            if previous.get('digest') != digest:
                raise GetNoteError('conflict', 'This save request was used for other content.')
            if previous.get('result'):
                return previous['result']
            raise GetNoteError('unknown_result', 'This Notion save is unconfirmed. Duplicate creation is blocked; check Notion first.')
        if len(ledger) >= 10000:
            raise GetNoteError('receipt_limit', 'The local Notion receipt limit has been reached. Review existing receipts first.')
        with Session() as session:
            payload = _payload(session, title, content, tags, target)
            ledger[request_id] = {'digest': digest}
            _store_ledger(ledger)
            try:
                result = session.request('POST', 'pages', payload)
                ident = str(uuid.UUID(result.get('id', '')))
            except GetNoteError as error:
                if error.code == 'notion_rejected':
                    del ledger[request_id]
                    _store_ledger(ledger)
                raise
            except (ValueError, TypeError, AttributeError):
                raise GetNoteError('unknown_result', 'Notion did not confirm the new page ID. Check the result in Notion.') from None
            saved = {'ok': True, 'id': ident, 'url': 'https://www.notion.so/' + ident.replace('-', ''), 'destination': 'Notion', 'tags': tags}
            ledger[request_id]['result'] = saved
            _store_ledger(ledger)
            return saved
