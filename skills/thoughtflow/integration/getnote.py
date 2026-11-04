"""Connect to the user's existing Codex GetNote MCP server over stdio.

Credentials remain in the user's existing Codex/MCP configuration or inherited
process environment. They never enter HTTP responses, source files, or logs.
Importing this module performs no network requests and never creates a note.
"""
from __future__ import annotations

import ast
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

try:
    import tomllib  # type: ignore[import-not-found]  # Python >=3.11
except ImportError:
    tomllib = None

_MAX_MESSAGE_BYTES = 2 * 1024 * 1024
_STATUS_TTL_SECONDS = 30.0
_PROTOCOL_VERSION = "2025-03-26"
_cache_lock = threading.Lock()
_cached_status: Optional[Dict[str, Any]] = None
_cache_until = 0.0
_REQUEST_ID = re.compile(r"^[\x21-\x7e]{1,128}$")
_DECIMAL_ID = re.compile(r"^[0-9]{1,32}$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class GetNoteError(Exception):
    """A safe-to-display failure, without external server output or credentials."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.public_message = message


def _fail(code: str, message: str) -> GetNoteError:
    return GetNoteError(code, message)


def _parse_fallback(text: str, server_name: str = 'getnote') -> Dict[str, Any]:
    """Parse the ordinary stdio getnote section when tomllib isn't installed.

    We intentionally accept only string values, lists of strings, booleans, and
    an optional env table. Other TOML features require Python 3.11+ and fail
    closed rather than guessing. Nothing from the file is printed.
    """
    section = None
    result: Dict[str, Any] = {}
    env: Dict[str, str] = {}
    lines = iter(text.splitlines())
    for line in lines:
        escaped = re.escape(server_name)
        header = re.match(r'^\s*\[\s*mcp_servers\.(?:"' + escaped + r'"|\'' + escaped + r'\'|' + escaped + r')(\.env)?\s*\]\s*(?:#.*)?$', line)
        if header:
            section = "env" if header.group(1) else "server"
            continue
        if re.match(r'^\s*\[', line):
            section = None
            continue
        if section is None or not line.strip() or line.lstrip().startswith("#"):
            continue
        match = re.match(r'^\s*([A-Za-z0-9_-]+)\s*=\s*(.*)$', line)
        if not match:
            raise _fail("unsupported_config", "Unsupported GetNote MCP configuration format. Use Python 3.11 or later.")
        name, value = match.groups()
        if section == "server" and name not in ("command", "args", "cwd", "enabled"):
            continue
        if section == "env" and not _ENV_NAME.fullmatch(name):
            raise _fail("unsupported_config", "Unsupported GetNote MCP environment configuration.")
        while True:
            try:
                parsed = {"true": True, "false": False}.get(value.strip())
                if parsed is None:
                    parsed = ast.literal_eval(value)
                break
            except (ValueError, SyntaxError):
                if value.lstrip().startswith("[") and not value.rstrip().endswith("]"):
                    try:
                        value += "\n" + next(lines)
                        continue
                    except StopIteration:
                        pass
                raise _fail("unsupported_config", "Unsupported GetNote MCP configuration format. Use Python 3.11 or later.") from None
        if section == "env":
            if not isinstance(parsed, str):
                raise _fail("unsupported_config", "Unsupported GetNote MCP environment configuration.")
            env[name] = parsed
        else:
            result[name] = parsed
    if env:
        result["env"] = env
    return result


def _server_config() -> Dict[str, Any]:
    config_path = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "config.toml"
    try:
        if tomllib:
            with config_path.open("rb") as f:
                value = tomllib.load(f).get("mcp_servers", {}).get("getnote", {})
        else:
            value = _parse_fallback(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _fail("not_configured", "GetNote MCP configuration not found. Connect GetNote in Codex first.") from None
    except GetNoteError:
        raise
    except Exception:
        raise _fail("invalid_config", "Cannot read GetNote MCP configuration. Check your Codex configuration.") from None
    if not isinstance(value, dict) or not value:
        raise _fail("not_configured", "GetNote MCP configuration not found. Connect GetNote in Codex first.")
    if value.get("enabled", True) is False:
        raise _fail("disabled", "GetNote MCP is disabled. Enable the connection in Codex first.")
    command, args, env = value.get("command"), value.get("args", []), value.get("env", {})
    if not isinstance(command, str) or not command or not isinstance(args, list) or not all(isinstance(x, str) for x in args):
        raise _fail("unsupported_transport", "Only local stdio GetNote MCP connections are supported.")
    if not isinstance(env, dict) or not all(isinstance(k, str) and _ENV_NAME.fullmatch(k) and isinstance(v, str) for k, v in env.items()):
        raise _fail("invalid_config", "Invalid GetNote MCP environment configuration.")
    if not shutil.which(command):
        raise _fail("runtime_unavailable", "The GetNote MCP runtime was not found. Check the local environment.")
    cwd = value.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not Path(cwd).is_dir()):
        raise _fail("invalid_config", "The GetNote MCP working directory is unavailable.")
    return {"command": command, "args": args, "env": env, "cwd": cwd}


class _MCP:
    def __init__(self, config: Dict[str, Any], timeout: float = 12.0):
        self.timeout = timeout
        self._lines: queue.Queue = queue.Queue()
        self._next_id = 0
        self.process: Optional[subprocess.Popen] = None
        try:
            self.process = subprocess.Popen(
                [config["command"], *config["args"]],
                cwd=config["cwd"],
                env={**os.environ, **config["env"]},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except (OSError, ValueError):
            raise _fail("start_failed", "Cannot start GetNote. Check the local MCP configuration.") from None
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                line = self.process.stdout.readline(_MAX_MESSAGE_BYTES + 1)
                if not line:
                    break
                if len(line) > _MAX_MESSAGE_BYTES:
                    self._lines.put(None)
                    break
                try:
                    self._lines.put(json.loads(line))
                except (ValueError, UnicodeDecodeError):
                    # Stdout may include non-protocol startup messages.
                    continue
        finally:
            self._lines.put(None)

    def _send(self, request: Dict[str, Any]) -> None:
        assert self.process and self.process.stdin
        try:
            payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
            self.process.stdin.write(payload)
            self.process.stdin.flush()
        except (OSError, ValueError, BrokenPipeError):
            raise _fail("connection_lost", "GetNote disconnected. Try again later.") from None

    def request(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                response = self._lines.get(timeout=max(0.001, deadline - time.monotonic()))
            except queue.Empty:
                raise _fail("timeout", "GetNote timed out. If the save result is unknown, retry the same request.") from None
            if response is None:
                raise _fail("connection_lost", "GetNote disconnected. Try again later.")
            if not isinstance(response, dict) or response.get("id") != request_id:
                if time.monotonic() >= deadline:
                    raise _fail("timeout", "GetNote timed out. If the save result is unknown, retry the same request.")
                continue
            if "error" in response or not isinstance(response.get("result"), dict):
                raise _fail("protocol_error", "Unexpected GetNote response. Check the MCP configuration.")
            return response["result"]

    def initialize(self) -> None:
        self.request("initialize", {"protocolVersion": _PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "thought-flow", "version": "0.1.0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def tool(self, name: str, arguments: Dict[str, Any]) -> Any:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        contents = result.get("content", [])
        if result.get("isError"):
            # Never return arbitrary server messages: they may contain secrets or note text.
            raise _fail("tool_error", "GetNote could not complete the request. Check the connection, permissions and quota.")
        if not isinstance(contents, list):
            raise _fail("protocol_error", "Unexpected GetNote response. Check the MCP configuration.")
        for entry in contents:
            if isinstance(entry, dict) and entry.get("type") == "text" and isinstance(entry.get("text"), str):
                try:
                    return json.loads(entry["text"])
                except ValueError:
                    break
        raise _fail("protocol_error", "GetNote returned unrecognized data.")

    def close(self) -> None:
        if not self.process:
            return
        proc = self.process
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
        for pipe in (proc.stdin, proc.stdout):
            if pipe:
                try:
                    pipe.close()
                except OSError:
                    pass

    def __enter__(self) -> "_MCP":
        return self

    def __exit__(self, *unused: Any) -> None:
        self.close()


def status(*, refresh: bool = False) -> Dict[str, Any]:
    """Check the actual connection with a read-only quota request; cache briefly."""
    global _cached_status, _cache_until
    with _cache_lock:
        if not refresh and _cached_status is not None and time.monotonic() < _cache_until:
            return dict(_cached_status)
        try:
            config = _server_config()
            with _MCP(config) as mcp:
                mcp.initialize()
                response = mcp.request("tools/list", {})
                tools = response.get("tools", [])
                names = {item.get("name") for item in tools if isinstance(item, dict)} if isinstance(tools, list) else set()
                if not {"save_note", "get_quota"}.issubset(names):
                    raise _fail("missing_tool", "GetNote MCP does not provide note saving.")
                quota = mcp.tool("get_quota", {})
                if not isinstance(quota, dict):
                    raise _fail("protocol_error", "GetNote returned unrecognized data.")
            current = {"connected": True, "available": True, "destination": "GetNote"}
        except GetNoteError as exc:
            current = {"connected": False, "available": exc.code not in {"not_configured", "disabled", "unsupported_transport", "runtime_unavailable", "invalid_config", "unsupported_config"}, "reason": exc.public_message}
        except Exception:
            current = {"connected": False, "available": False, "reason": "Cannot verify GetNote. Check the local configuration."}
        _cached_status = current
        _cache_until = time.monotonic() + _STATUS_TTL_SECONDS
        return dict(current)


def save_note(title: str, content: str, client_request_id: Optional[str] = None, tags=None) -> Dict[str, Any]:
    """Create one *personal note* after the local user confirms the preview.

    Reuse client_request_id when retrying an uncertain response; this protects
    against duplicate creations. No knowledge-base topic is selected implicitly.
    """
    if not isinstance(title, str) or not title.strip() or len(title) > 500:
        raise _fail("invalid_input", "The note title must contain 1 to 500 characters.")
    if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 512 * 1024:
        raise _fail("invalid_input", "The note body cannot be empty or exceed 512 KB.")
    request_id = client_request_id or str(uuid.uuid4())
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise _fail("invalid_input", "Invalid save request ID. Reopen the save preview.")
    from integration.common import validate_tags
    tags = validate_tags(tags)
    config = _server_config()
    with _MCP(config) as mcp:
        mcp.initialize()
        arguments = {"note_type": "plain_text", "title": title.strip(), "content": content, "client_request_id": request_id}
        if tags:
            arguments['tags'] = tags
        result = mcp.tool("save_note", arguments)
    if not isinstance(result, dict):
        raise _fail("unknown_result", "Save not confirmed. Retry the same request to avoid duplicates.")
    raw_id = result.get("note_id", result.get("id"))
    note_id = str(raw_id) if isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool) else ""
    if not _DECIMAL_ID.fullmatch(note_id):
        raise _fail("unknown_result", "Save not confirmed. Retry the same request to avoid duplicates.")
    return {"ok": True, "id": note_id, "url": "https://biji.com/note/" + note_id, "destination": "GetNote"}
