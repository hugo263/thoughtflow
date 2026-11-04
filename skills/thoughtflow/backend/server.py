#!/usr/bin/env python3
"""Loopback-only Thought Flow server; reads local Codex history on demand."""
from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parent.parent
if str(PROJECT) not in sys.path:
    sys.path.insert(0, str(PROJECT))

from backend.history import CompressedSessionReader, HistoryReadError, valid_thread_id
from backend.imports import MixedHistoryStore  # noqa: E402
from backend.organize import Organizer, OrganizeError  # noqa: E402
from integration import hub, notion  # noqa: E402
from integration.common import validate_tags, valid_target  # noqa: E402

try:
    from integration import getnote  # type: ignore  # noqa: E402
except ImportError:
    getnote = None

SERVICE = "thought-flow"
WEB_ROOT = (PROJECT / "web").resolve()
MAX_BODY = 2 * 1024 * 1024
MAX_NOTE_BYTES = 512 * 1024
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
STREAM_LIMIT = threading.BoundedSemaphore(8)
SAVE_LOCK = threading.Lock()
SAVED_REQUESTS: dict[str, tuple[float, str, dict[str, Any]]] = {}


class ThoughtFlowServer(ThreadingHTTPServer):
    daemon_threads = True
    # Restarting on the same loopback port must survive closed SSE connections
    # in TIME_WAIT. SO_REUSEPORT remains disabled: an active listener still wins.
    allow_reuse_address = True

    def __init__(self, port: int, store: HistoryStore | None = None):
        self.store = store or MixedHistoryStore()
        self.organizer = Organizer()
        super().__init__(("127.0.0.1", port), Handler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        # Exception traces can accidentally contain user conversation text.
        return


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ThoughtFlow"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # Conversation identifiers and content must not be written to logs.
        return

    def _headers(self, content_type: str, length: int | None = None) -> None:
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")

    def _json(self, status: int, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._headers("application/json; charset=utf-8", len(payload))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)

    def _safe_origin(self, *, require_origin: bool = False) -> bool:
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1 or hosts[0].lower() not in allowed:
            return False
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1 or (require_origin and len(origins) != 1):
            return False
        if origins and origins[0] != "http://" + hosts[0].lower():
            return False
        if self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
            return False
        return True

    def _path(self) -> str | None:
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.scheme or parsed.netloc or not parsed.path.startswith("/"):
            return None
        try:
            return urllib.parse.unquote(parsed.path, errors="strict")
        except (UnicodeDecodeError, ValueError):
            return None

    def do_GET(self) -> None:
        if not self._safe_origin():
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid request origin."})
            return
        path = self._path()
        if path is None:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request address."})
            return
        if path == "/api/health":
            self._json(HTTPStatus.OK, {"ok": True, "service": SERVICE, "pid": os.getpid()})
            return
        if path == "/api/conversations":
            try:
                self._json(HTTPStatus.OK, {"conversations": self.server.store.refresh()})
            except (OSError, ValueError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Cannot read the local Codex conversation directory."})
            return
        if path == "/api/integrations":
            self._json(HTTPStatus.OK, hub.status())
            return
        if path == '/api/integrations/notion/destinations':
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('q', [''])[0]
            try:
                self._json(HTTPStatus.OK, notion.destinations(query))
            except getnote.GetNoteError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': exc.public_message, 'code': exc.code})
            except Exception:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'Notion destinations are temporarily unavailable.'})
            return
        organized = re.fullmatch(r'/api/conversations/([0-9a-fA-F-]{36})/organized', path)
        if organized:
            ident = organized.group(1)
            try:
                self.server.store.get(ident)
                self._json(HTTPStatus.OK, {'job': self.server.organizer.current(ident)})
            except (KeyError, FileNotFoundError):
                self._json(HTTPStatus.NOT_FOUND, {'error': 'Conversation not found.'})
            except HistoryReadError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': exc.public_message, 'code': exc.code})
            except (OSError, ValueError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'The summary is temporarily unavailable.'})
            return
        match = re.fullmatch(r"/api/conversations/([0-9a-fA-F-]{36})(/events)?", path)
        if match:
            ident, events = match.groups()
            if not valid_thread_id(ident):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Conversation not found."})
                return
            try:
                conversation, reader = self.server.store.get(ident)
                if events:
                    self._events(ident, reader)
                else:
                    revision, nodes = reader.snapshot()
                    self._json(HTTPStatus.OK, {"conversation": conversation, "nodes": nodes, "revision": revision})
            except HistoryReadError as exc:
                if exc.code in {"zstd_too_large", "record_too_large", "history_too_large"}:
                    code = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                elif exc.code in {"zstd_invalid", "invalid_history"}:
                    code = HTTPStatus.UNPROCESSABLE_ENTITY
                elif exc.code == "zstd_timeout":
                    code = HTTPStatus.GATEWAY_TIMEOUT
                else:
                    code = HTTPStatus.SERVICE_UNAVAILABLE
                self._json(code, {"error": exc.public_message, "code": exc.code})
            except (KeyError, FileNotFoundError):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Conversation not found."})
            except (OSError, ValueError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "This conversation is temporarily unavailable."})
            return
        if path.startswith("/api/"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
            return
        self._static(path)

    def _static(self, path: str) -> None:
        relative = "index.html" if path == "/" else path.lstrip("/")
        if "\x00" in relative or "\\" in relative or any(part in {".", ".."} for part in Path(relative).parts):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
            return
        try:
            file = (WEB_ROOT / relative).resolve(strict=True)
            if not file.is_relative_to(WEB_ROOT) or not file.is_file():
                raise FileNotFoundError
            payload = file.read_bytes()
        except (OSError, RuntimeError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Page not found."})
            return
        media = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        if media.startswith("text/") or media in {"application/javascript", "application/json"}:
            media += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self._headers(media, len(payload))
        self.end_headers()
        self.wfile.write(payload)

    def _events(self, ident: str, reader: Any) -> None:
        if not STREAM_LIMIT.acquire(blocking=False):
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Too many live connections. Close unused pages."})
            return
        try:
            revision = reader.sync()
            self.send_response(HTTPStatus.OK)
            self._headers("text/event-stream; charset=utf-8")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            # Refresh once after opening to close the GET/SSE connection race.
            self.wfile.write(b'data: {"type":"update"}\n\n')
            self.wfile.flush()
            heartbeat = time.monotonic()
            while True:
                time.sleep(1)
                try:
                    if isinstance(reader, CompressedSessionReader):
                        rebound = self.server.store.rebind(ident, reader)
                        if rebound is not reader:
                            reader = rebound
                            revision = reader.sync()
                            self.wfile.write(b'data: {"type":"update"}\n\n')
                            self.wfile.flush()
                            heartbeat = time.monotonic()
                            continue
                    current = reader.sync()
                except (FileNotFoundError, PermissionError, OSError):
                    try:
                        rebound = self.server.store.rebind(ident, reader, force=True)
                        if rebound is not reader:
                            reader = rebound
                            revision = reader.sync()
                            self.wfile.write(b'data: {"type":"update"}\n\n')
                            self.wfile.flush()
                            heartbeat = time.monotonic()
                            continue
                    except (KeyError, OSError, HistoryReadError):
                        pass
                    self.wfile.write(b'data: {"type":"unavailable"}\n\n')
                    self.wfile.flush()
                    break
                except (KeyError, HistoryReadError):
                    # Prompt the client to reload and display the explicit error.
                    self.wfile.write(b'data: {"type":"update"}\n\n')
                    self.wfile.flush()
                    break
                if current != revision:
                    revision = current
                    self.wfile.write(b'data: {"type":"update"}\n\n')
                    self.wfile.flush()
                    heartbeat = time.monotonic()
                elif time.monotonic() - heartbeat >= 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    heartbeat = time.monotonic()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            pass
        finally:
            STREAM_LIMIT.release()
            self.close_connection = True

    def do_POST(self) -> None:
        if not self._safe_origin(require_origin=True):
            self.close_connection = True
            self._json(HTTPStatus.FORBIDDEN, {"error": "Invalid request origin."})
            return
        path = self._path()
        save_path = re.fullmatch(r'/api/integrations/(getnote|obsidian|notion)/save', path or '')
        organize_path = re.fullmatch(r'/api/conversations/([0-9a-fA-F-]{36})/organize', path or '')
        if not save_path and not organize_path and path != "/api/open-external":
            self.close_connection = True
            self._json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found."})
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self.close_connection = True
            self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "The request must use JSON."})
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request format."})
            return
        lengths = self.headers.get_all("Content-Length", [])
        try:
            if len(lengths) != 1 or not lengths[0].isdigit():
                raise ValueError
            length = int(lengths[0])
            body_limit = 4096 if path == "/api/open-external" or organize_path else MAX_BODY
            if length <= 0 or length > body_limit:
                raise ValueError
            request = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            self.close_connection = True
            self._json(HTTPStatus.BAD_REQUEST, {"error": "The request body is invalid or too large."})
            return
        if not isinstance(request, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid request body."})
            return
        if path == "/api/open-external":
            self._open_external(request)
            return
        if organize_path:
            if set(request) - {'sourceKey', 'mode'} or not isinstance(request.get('sourceKey'), str) or not re.fullmatch(r'[0-9a-f]{16}', request['sourceKey']):
                self._json(HTTPStatus.BAD_REQUEST, {'error': 'Invalid snapshot ID. Refresh and try again.'})
                return
            try:
                conversation, reader = self.server.store.get(organize_path.group(1))
                _, nodes = reader.snapshot()
                job = self.server.organizer.start(conversation, nodes, request['sourceKey'], mode=request.get('mode', 'ai'))
                self._json(HTTPStatus.ACCEPTED if job['status'] == 'running' else HTTPStatus.OK, {'job': job})
            except OrganizeError as exc:
                status = HTTPStatus.CONFLICT if exc.code in {'stale_source', 'busy'} else HTTPStatus.REQUEST_ENTITY_TOO_LARGE if exc.code == 'too_large' else HTTPStatus.SERVICE_UNAVAILABLE
                self._json(status, {'error': exc.public_message, 'code': exc.code})
            except (KeyError, FileNotFoundError):
                self._json(HTTPStatus.NOT_FOUND, {'error': 'Conversation not found.'})
            except HistoryReadError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': exc.public_message, 'code': exc.code})
            except (OSError, ValueError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {'error': 'Source content unavailable. Reorganization was not started.'})
            return
        provider = save_path.group(1)
        title, content, request_id = (request.get(key) for key in ("title", "content", "clientRequestId"))
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "The note title must contain 1 to 200 characters."})
            return
        if not isinstance(content, str) or not content.strip():
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Note content cannot be empty."})
            return
        try:
            title.encode("utf-8")
            note_bytes = content.encode("utf-8")
        except UnicodeEncodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "The note contains invalid characters. Check it and try again."})
            return
        if len(content) > 200000 or len(note_bytes) > MAX_NOTE_BYTES:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "The note body cannot exceed 200,000 characters or 512 KiB."})
            return
        if not isinstance(request_id, str) or not TOKEN_PATTERN.fullmatch(request_id):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "A valid save request ID is required."})
            return
        try:
            tags = validate_tags(request.get('tags'))
        except getnote.GetNoteError as exc:
            self._json(HTTPStatus.BAD_REQUEST, {'error': exc.public_message})
            return
        target = request.get('targetId')
        if (provider != 'getnote' and not valid_target(target)) or (provider == 'getnote' and target is not None):
            self._json(HTTPStatus.BAD_REQUEST, {'error': 'Choose a valid save destination.'})
            return
        ident, node_id = request.get("conversationId"), request.get("nodeId")
        if ident is not None and (not isinstance(ident, str) or not valid_thread_id(ident)):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid conversation ID."})
            return
        if node_id is not None and (not isinstance(node_id, str) or not TOKEN_PATTERN.fullmatch(node_id)):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid card ID."})
            return
        if getnote is None:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "The GetNote adapter is not installed."})
            return
        digest = hashlib.sha256(json.dumps([provider, target, title, content, tags], ensure_ascii=False).encode("utf-8")).hexdigest()
        with SAVE_LOCK:
            now = time.monotonic()
            for key, (created, _, _) in list(SAVED_REQUESTS.items()):
                if now - created > 3600:
                    del SAVED_REQUESTS[key]
            existing = SAVED_REQUESTS.get(request_id)
            if existing:
                if existing[1] != digest:
                    self._json(HTTPStatus.CONFLICT, {"error": "This request ID was used for different content. Start a new save."})
                else:
                    self._json(HTTPStatus.OK, existing[2])
                return
            try:
                result = hub.save_note(provider, title, content, request_id, tags=tags, target=target)
                if not isinstance(result, dict) or result.get("ok") is not True:
                    self._json(HTTPStatus.BAD_GATEWAY, {"error": "The destination did not confirm the save. Check the connection."})
                    return
                safe_result = {key: result[key] for key in ("ok", "id", "url", "destination") if key in result and isinstance(result[key], (str, bool))}
                if 'tags' in request:
                    safe_result['tags'] = tags
                SAVED_REQUESTS[request_id] = (now, digest, safe_result)
                self._json(HTTPStatus.OK, safe_result)
            except getnote.GetNoteError as exc:
                status = HTTPStatus.GATEWAY_TIMEOUT if exc.code == "timeout" else HTTPStatus.CONFLICT if exc.code == 'conflict' else HTTPStatus.BAD_REQUEST if exc.code in {'invalid_input', 'invalid_target'} else HTTPStatus.BAD_GATEWAY
                self._json(status, {"error": exc.public_message, "code": exc.code})
            except Exception:
                self._json(HTTPStatus.BAD_GATEWAY, {"error": "Save failed. Check the connection, then retry the same request."})

    def _open_external(self, request: dict[str, Any]) -> None:
        """Open only this server's own viewer in the system-default browser."""
        if set(request) != {"conversationId"}:
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Only the current conversation ID is supported."})
            return
        ident = request["conversationId"]
        if ident is not None:
            if not isinstance(ident, str) or not valid_thread_id(ident):
                self._json(HTTPStatus.BAD_REQUEST, {"error": "Invalid conversation ID."})
                return
            ident = ident.lower()
            try:
                self.server.store.get(ident)
            except (KeyError, FileNotFoundError):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Conversation not found."})
                return
            except (OSError, ValueError):
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "This conversation is temporarily unavailable."})
                return
        url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        if ident is not None:
            url += "?" + urllib.parse.urlencode({"thread": ident})
        try:
            if not webbrowser.open(url, new=2):
                raise RuntimeError("No default browser was available")
        except Exception:
            self._json(HTTPStatus.BAD_GATEWAY, {"error": "Cannot open the default browser. Check browser settings and try again."})
            return
        self._json(HTTPStatus.OK, {"ok": True})


def main() -> None:
    parser = argparse.ArgumentParser(description="Start the local Thought Flow viewer")
    parser.add_argument("--port", type=int, default=0, help="Loopback port; 0 selects a free port")
    parser.add_argument("--open", action="store_true", help="Open the viewer in the default browser")
    parser.add_argument("--thread", help="Select a Codex conversation UUID when the page opens")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    if args.thread and not valid_thread_id(args.thread):
        parser.error("thread must be a Codex conversation UUID")
    server = ThoughtFlowServer(args.port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    if args.thread:
        url += "?" + urllib.parse.urlencode({"thread": args.thread})
    print(url, flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
