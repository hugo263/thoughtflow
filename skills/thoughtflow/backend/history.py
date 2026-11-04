"""Read the user's local Codex rollouts without modifying them.

This is deliberately a narrow reader for persisted, user-visible messages and
completed tool items. The private Reasoning item and encrypted model content
are not inspected or returned. Event availability depends on the Codex version.
"""
from __future__ import annotations

import copy
import collections
import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from backend import zstd_read

UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
FILENAME_ID = re.compile(r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl(?:\.zst)?$")
AMBIENT = re.compile(r"<in-app-browser-context\b(?=[^>]*\bsource\s*=\s*['\"]ambient-ui-state['\"])[^>]*>.*?</in-app-browser-context>\s*", re.S | re.I)
ENVIRONMENT = re.compile(r"<environment_context\b[^>]*>.*?</environment_context>\s*", re.S | re.I)
REQUEST_HEADER = re.compile(r"^\s*##[ \t]*My request:[ \t]*(?:\r?\n|$)", re.I)
REQUEST_MARKER = re.compile(r"^##[ \t]*My request:[ \t]*\r?$", re.I | re.M)
REFERENCE_HEADER = re.compile(r"^\s*##[ \t]*Referenced chats with Codex:[ \t]*\r?\n([^\r\n]*)\r?\n", re.I)
MAX_LINE = 8 * 1024 * 1024
MAX_QUESTION = 16000
MAX_RAW_QUESTION = 256000
MAX_ANSWER = 200000
MAX_TOOL_DETAIL = 8000
MAX_RETAINED_CHARS = 8 * 1024 * 1024
MAX_NODES = 2000
MAX_TOOLS = 10000


class HistoryReadError(Exception):
    def __init__(self, code: str, public_message: str):
        super().__init__(public_message)
        self.code = code
        self.public_message = public_message


def valid_thread_id(value: str) -> bool:
    return bool(UUID_PATTERN.fullmatch(value))


def _question_reply(value: str) -> list[dict[str, str]]:
    reply = re.fullmatch(r'<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>', value.strip(), re.S)
    if not reply:
        return []
    try:
        answers = json.loads(reply.group(1))
    except (ValueError, RecursionError):
        return []
    if not (isinstance(answers, list) and answers and all(isinstance(item, dict) and isinstance(item.get('question'), str) and isinstance(item.get('answer'), str) for item in answers)):
        return []
    return [{'question': item['question'].strip(), 'answer': item['answer'].strip()} for item in answers]


def _clean_question(value: str, decode_reply: bool = True) -> str:
    # The host can prepend its own context before the actual user message.
    # Only consume recognized leading blocks; never strip matching markup
    # from text the user wrote after the request marker.
    host_context = False
    while True:
        candidate = value.lstrip()
        match = AMBIENT.match(candidate) or ENVIRONMENT.match(candidate)
        if not match:
            break
        value = candidate[match.end():]
        host_context = True
    reference = REFERENCE_HEADER.match(value)
    if reference:
        instruction = reference.group(1).lower()
        marker = REQUEST_MARKER.search(value, reference.end())
        if (instruction.startswith("these are live references") and "you must" in instruction
                and "read_thread" in instruction and marker):
            metadata = value[reference.end():marker.start()]
            metadata = AMBIENT.sub("", metadata)
            metadata = ENVIRONMENT.sub("", metadata)
            try:
                references = json.loads(metadata.strip())
            except (ValueError, RecursionError):
                references = None
            if (isinstance(references, list) and references
                    and all(isinstance(item, dict) and isinstance(item.get("threadId"), str)
                            and item["threadId"] for item in references)):
                value = value[marker.end():]
                host_context = False
    if host_context:
        value = REQUEST_HEADER.sub("", value)
    value = value.strip()
    answers = _question_reply(value) if decode_reply else []
    if answers:
        value = '\n\n'.join(item['question'] + '\n' + item['answer'] for item in answers)
    return value if len(value) <= MAX_QUESTION else value[:MAX_QUESTION] + "\n… (Question truncated because it is too long.)"


def _text(value: Any, limit: int = MAX_TOOL_DETAIL) -> str:
    """Extract readable text without copying image/audio blobs into responses."""
    parts: list[str] = []
    used = 0

    def add(part: str) -> None:
        nonlocal used
        if not part or used >= limit:
            return
        remaining = limit - used
        parts.append(part[:remaining])
        used += min(len(part), remaining)

    def walk(obj: Any, depth: int = 0) -> None:
        if obj is None or used >= limit or depth > 6:
            return
        if isinstance(obj, str):
            add(obj)
        elif isinstance(obj, (int, float, bool)):
            add(str(obj))
        elif isinstance(obj, list):
            for entry in obj[:100]:
                if used >= limit:
                    break
                if parts:
                    add("\n")
                walk(entry, depth + 1)
        elif isinstance(obj, dict):
            kind = str(obj.get("type", "")).lower()
            if kind in {"image", "audio", "input_image", "output_image", "blob"}:
                add("[Image or audio content]")
            elif isinstance(obj.get("text"), str):
                add(obj["text"])
            elif "content" in obj:
                walk(obj["content"], depth + 1)
            else:
                for key, item in list(obj.items())[:50]:
                    if key in {"data", "blob", "base64", "encrypted_content", "raw_content", "_meta"}:
                        continue
                    if parts:
                        add("\n")
                    add(str(key) + ": ")
                    walk(item, depth + 1)

    walk(value)
    text = "".join(parts).strip()
    if used >= limit:
        text += "\n… (Content truncated because it is too long.)"
    return text


def _iso_time(mtime: float) -> str:
    return dt.datetime.fromtimestamp(mtime, dt.timezone.utc).isoformat()


def _status(value: Any, error: bool = False) -> str:
    text = str(value or "").lower()
    if error or text in {"failed", "error", "cancelled", "canceled", "interrupted"}:
        return "failed"
    if text in {"running", "inprogress", "in_progress", "started", "pending"}:
        return "running"
    return "completed"


class RolloutParser:
    def __init__(self, structured: bool):
        self.structured = structured
        self.nodes: list[dict[str, Any]] = []
        self.by_id: dict[str, dict[str, Any]] = {}
        self.current: dict[str, Any] | None = None
        self.revision = 0
        self._synthetic = 0
        self._retained_chars = 0
        self._tool_count = 0

    def _touch(self) -> None:
        self.revision += 1

    def _retain(self, delta: int) -> None:
        if self._retained_chars + delta > MAX_RETAINED_CHARS:
            raise HistoryReadError("history_too_large", "Conversation text exceeds the 8 MiB memory limit. It was not read in full.")
        self._retained_chars = max(0, self._retained_chars + delta)

    def _new_node(self, ident: str, question: str, timestamp: str | None) -> None:
        replies = _question_reply(_clean_question(question, decode_reply=False))
        question = _clean_question(question)
        if not question:
            return
        if self.current and self.current["id"] == ident:
            return
        if (not self.structured and self.current and self.current["question"] == question
                and not self.current["_final"] and not self.current["_commentary"]):
            return
        if ident in self.by_id:
            return
        if len(self.nodes) >= MAX_NODES:
            raise HistoryReadError("history_too_large", "This conversation exceeds 2,000 turns. It was not read in full.")
        self._retain(len(question))
        # A user can steer an ongoing task with another message before the
        # original turn completes. Do not leave the older card running or call
        # it complete: its work has continued in the newly created card.
        if self.current and self.current["state"] == "running":
            self.current["state"] = "continued"
            self._touch()
        self.current = {
            "id": ident,
            "parentId": self.nodes[-1]["id"] if self.nodes else None,
            "question": question,
            "answer": "",
            "tools": [],
            "state": "running",
            "createdAt": timestamp,
            "_commentary": "",
            "_commentary_full": False,
            "_last_update_hash": None,
            "_final": "",
        }
        if replies:
            # Preserve only public question/answer pairs, never host call IDs.
            pairs = [{'question': item['question'][:2000], 'answer': item['answer'][:4000]} for item in replies[:20]]
            self._retain(sum(len(item['question']) + len(item['answer']) for item in pairs))
            self.current['inputReply'] = pairs
        self.nodes.append(self.current)
        self.by_id[ident] = self.current
        self._touch()

    def _message(self, text: str, phase: str | None = None) -> None:
        if not self.current:
            return
        if phase is not None:
            if not isinstance(phase, str):
                return
            phase = phase.lower()
            if phase not in {"", "commentary", "final", "final_answer"}:
                return
        text = text.strip()
        if len(text) > MAX_ANSWER:
            text = text[:MAX_ANSWER] + "\n… (Truncated)"
        if not text:
            return
        current = self.current
        if phase in {"final_answer", "final"}:
            if current["_final"] == text:
                return
            old_length = len(current["_final"] or current["_commentary"])
            self._retain(len(text) - old_length)
            current["_final"] = text
            current["_commentary"] = ""
            current["_commentary_full"] = False
            current["_last_update_hash"] = None
        else:
            fingerprint = hash(text)
            if current["_final"] or current["_commentary_full"] or current["_last_update_hash"] == fingerprint:
                return
            old = current["_commentary"]
            combined = old + ("\n\n" if old else "") + text
            if len(combined) > MAX_ANSWER:
                combined = combined[:MAX_ANSWER] + "\n… (Truncated)"
                current["_commentary_full"] = True
            self._retain(len(combined) - len(old))
            current["_commentary"] = combined
            current["_last_update_hash"] = fingerprint
        current["answer"] = current["_final"] or current["_commentary"]
        self._touch()

    def _finish(self) -> None:
        if self.current and self.current["state"] != "completed":
            self.current["state"] = "completed"
            self._touch()

    def _input_request(self, item: dict[str, Any]) -> None:
        if not self.current or str(item.get('name', '')).rsplit('.', 1)[-1] not in {'request_user_input', 'request_user_input_async'}:
            return
        args = item.get('arguments')
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, RecursionError):
                return
        if not isinstance(args, dict) or not isinstance(args.get('questions'), list):
            return
        prompts = self.current.setdefault('inputRequests', [])
        for question in args['questions'][:20]:
            if not isinstance(question, dict):
                continue
            title = question.get('question') or question.get('title')
            if not isinstance(title, str) or not title.strip():
                continue
            prompt = title.strip()[:2000]
            options = question.get('options')
            if isinstance(options, list):
                for option in options[:8]:
                    label = option if isinstance(option, str) else option.get('label') if isinstance(option, dict) else None
                    if isinstance(label, str):
                        prompt += '\n- ' + label[:500]
            if prompt not in prompts and len(prompts) < 20:
                self._retain(len(prompt))
                prompts.append(prompt)
                self._touch()

    def _tool(self, item: dict[str, Any]) -> None:
        if not self.current:
            return
        kind = item.get("type", "")
        ident = str(item.get("id") or f"tool-{len(self.current['tools']) + 1}")[:200]
        label = ""
        detail = ""
        if kind in {"McpToolCall", "mcpToolCall", "DynamicToolCall", "dynamicToolCall"}:
            server, tool = str(item.get("server") or ""), str(item.get("tool") or item.get("toolName") or "Tool")
            label = "/".join(part for part in (server, tool) if part)
            args = _text(item.get("arguments"), 2500)
            output = _text(item.get("result") or item.get("output") or item.get("error"), 5200)
            detail = ("Input:\n" + args if args else "") + ("\n\nResult:\n" + output if output else "")
        elif kind in {"CommandExecution", "commandExecution"}:
            label = "Run command"
            command = _text(item.get("command"), 1800)
            output = _text(item.get("aggregated_output") or item.get("aggregatedOutput"), 6000)
            detail = ("Command:\n" + command if command else "") + ("\n\nOutput:\n" + output if output else "")
            if item.get("exit_code", item.get("exitCode")) is not None:
                detail += "\n\nExit code: " + str(item.get("exit_code", item.get("exitCode")))
        elif kind in {"FileChange", "fileChange"}:
            label = "Edit file"
            detail = _text(item.get("changes"), MAX_TOOL_DETAIL)
        elif kind in {"Extension", "extension"}:
            label = str(item.get("action") or item.get("kind") or "Extension tool")[:160]
            detail = _text(item.get("query") or item.get("results"), MAX_TOOL_DETAIL)
        elif kind in {"ImageView", "imageView"}:
            label = "View image"
            detail = _text(item.get("path"), 1200)
        elif kind in {"SubAgentActivity", "subAgentActivity"}:
            label = "Collaboration task"
            detail = _text({key: item[key] for key in ("kind", "agent_path") if key in item}, 1200)
        else:
            return
        result = item.get("result")
        failed = bool(item.get("error")) or (isinstance(result, dict) and bool(result.get("isError")))
        exit_code = item.get("exit_code", item.get("exitCode"))
        if isinstance(exit_code, int) and exit_code != 0:
            failed = True
        tool_record = {"id": ident, "label": label[:200], "status": _status(item.get("status"), failed), "detail": detail.strip()[:MAX_TOOL_DETAIL]}
        for index, old in enumerate(self.current["tools"]):
            if old["id"] == ident:
                if old != tool_record:
                    self._retain(len(tool_record["label"]) + len(tool_record["detail"]) - len(old["label"]) - len(old["detail"]))
                    self.current["tools"][index] = tool_record
                    self._touch()
                return
        if self._tool_count >= MAX_TOOLS:
            raise HistoryReadError("history_too_large", "This conversation exceeds 10,000 tool records. It was not read in full.")
        self._retain(len(tool_record["label"]) + len(tool_record["detail"]))
        self._tool_count += 1
        self.current["tools"].append(tool_record)
        self._touch()

    def feed(self, record: dict[str, Any]) -> None:
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        kind, event_type = record.get("type"), payload.get("type")
        timestamp = record.get("timestamp")
        if kind == 'response_item' and event_type == 'function_call':
            # User-visible input prompts can be absent from item_completed.
            # Inspect only these two public tool schemas, never Reasoning.
            self._input_request(payload)
        if kind == "event_msg":
            if event_type == "task_complete":
                self._finish()
                return
            if self.structured and event_type == "item_completed":
                item = payload.get("item")
                if not isinstance(item, dict):
                    return
                typ = item.get("type")
                if typ in {"UserMessage", "userMessage"}:
                    ident = str(item.get("id") or payload.get("turn_id") or "")
                    if not ident:
                        self._synthetic += 1
                        ident = f"turn-{self._synthetic}"
                    self._new_node(ident[:200], _text(item.get("content"), MAX_RAW_QUESTION), timestamp)
                elif typ in {"AgentMessage", "agentMessage"}:
                    self._message(_text(item.get("content") or item.get("text"), MAX_ANSWER), item.get("phase"))
                elif typ != "Reasoning" and typ != "reasoning":
                    self._tool(item)
                return
            if not self.structured and event_type == "user_message":
                self._synthetic += 1
                ident = str(payload.get("id") or payload.get("turn_id") or f"turn-{self._synthetic}")
                self._new_node(ident[:200], _text(payload.get("message") or payload.get("content"), MAX_RAW_QUESTION), timestamp)
                return
            if not self.structured and event_type == "agent_message":
                self._message(_text(payload.get("message") or payload.get("content"), MAX_ANSWER), payload.get("phase"))
                return
        if not self.structured and kind == "response_item" and event_type == "message":
            role = payload.get("role")
            if role == "user":
                self._synthetic += 1
                ident = str(payload.get("id") or f"turn-{self._synthetic}")
                self._new_node(ident[:200], _text(payload.get("content"), MAX_RAW_QUESTION), timestamp)
            elif role == "assistant":
                self._message(_text(payload.get("content"), MAX_ANSWER), payload.get("phase"))

    def snapshot(self) -> list[dict[str, Any]]:
        return [{key: copy.deepcopy(value) for key, value in node.items() if not key.startswith("_")} for node in self.nodes]


def _detect_structured(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            for _ in range(256):
                line = file.readline(MAX_LINE + 1)
                if not line:
                    break
                if len(line) > MAX_LINE:
                    while line and not line.endswith(b"\n"):
                        line = file.readline(MAX_LINE + 1)
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, UnicodeDecodeError, RecursionError):
                    continue
                if not isinstance(record, dict):
                    continue
                payload = record.get("payload") or {}
                if isinstance(payload, dict) and record.get("type") == "event_msg" and payload.get("type") == "item_completed":
                    return True
    except OSError:
        return False
    return False


class SessionReader:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.offset = 0
        self.identity: tuple[int, int] | None = None
        self.parser = RolloutParser(_detect_structured(path))
        self.version = 0
        self.failure: HistoryReadError | None = None

    def sync(self) -> int:
        with self.lock:
            stat = self.path.stat()
            identity = (stat.st_dev, stat.st_ino)
            reset = (self.identity is not None and identity != self.identity) or stat.st_size < self.offset
            if reset:
                self.offset = 0
                self.parser = RolloutParser(_detect_structured(self.path))
                self.failure = None
            if self.failure:
                raise self.failure
            self.identity = identity
            previous_revision = self.parser.revision
            with self.path.open("rb") as file:
                file.seek(self.offset)
                while True:
                    start = file.tell()
                    line = file.readline(MAX_LINE + 1)
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        if len(line) <= MAX_LINE:
                            file.seek(start)
                            break
                        # Skip an oversized record only after its terminating newline is present.
                        while line and not line.endswith(b"\n"):
                            line = file.readline(MAX_LINE + 1)
                        if not line:
                            file.seek(start)
                            break
                        self.offset = file.tell()
                        continue
                    self.offset = file.tell()
                    try:
                        record = json.loads(line)
                    except RecursionError:
                        self.failure = HistoryReadError("invalid_history", "Conversation records are nested too deeply. They were not read in full.")
                        raise self.failure from None
                    except (ValueError, UnicodeDecodeError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload") or {}
                    if (not self.parser.structured and isinstance(payload, dict)
                            and record.get("type") == "event_msg" and payload.get("type") == "item_completed"):
                        # A new live rollout may have appeared before the first structured item.
                        self.parser = RolloutParser(True)
                        self.offset = 0
                        self.version += 1
                        return self.sync()
                    try:
                        self.parser.feed(record)
                    except HistoryReadError as exc:
                        self.failure = exc
                        raise
            if reset or self.parser.revision != previous_revision:
                self.version += 1
            return self.version

    def snapshot(self) -> tuple[int, list[dict[str, Any]]]:
        with self.lock:
            version = self.sync()
            return version, self.parser.snapshot()


class _StructuredFormatDetected(Exception):
    pass


def _compressed_lines(path: Path):
    pending = bytearray()
    for chunk in zstd_read.iter_chunks(path):
        pending.extend(chunk)
        start = 0
        while True:
            end = pending.find(b"\n", start)
            if end < 0:
                break
            if end - start > MAX_LINE:
                raise HistoryReadError("record_too_large", "A compressed conversation record exceeds 8 MiB. It was not read in full.")
            yield bytes(pending[start:end])
            start = end + 1
        if start:
            del pending[:start]
        if len(pending) > MAX_LINE:
            raise HistoryReadError("record_too_large", "A compressed conversation record exceeds 8 MiB. It was not read in full.")
    if pending:
        yield bytes(pending)


def _read_compressed_pass(path: Path, force_structured: bool = False) -> RolloutParser:
    parser = RolloutParser(True) if force_structured else None
    pending: list[dict[str, Any]] = []
    pending_bytes = 0
    lines = _compressed_lines(path)
    try:
        for line in lines:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise HistoryReadError("zstd_invalid", "The compressed conversation contains incomplete or unrecognized records. It was not read in full.") from None
            if not isinstance(record, dict):
                raise HistoryReadError("zstd_invalid", "The compressed conversation contains unrecognized records. It was not read in full.")
            payload = record.get("payload") or {}
            structured = isinstance(payload, dict) and record.get("type") == "event_msg" and payload.get("type") == "item_completed"
            if parser is None:
                pending.append(record)
                pending_bytes += len(line)
                if structured or len(pending) >= 256 or pending_bytes >= 16 * 1024 * 1024:
                    parser = RolloutParser(structured)
                    for previous in pending:
                        parser.feed(previous)
                    pending.clear()
            else:
                if structured and not parser.structured:
                    raise _StructuredFormatDetected
                parser.feed(record)
        if parser is None:
            parser = RolloutParser(False)
            for previous in pending:
                parser.feed(previous)
        return parser
    finally:
        lines.close()


class CompressedSessionReader:
    """Decode the selected archived rollout once; re-read only if it changes."""
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.parser = RolloutParser(False)
        self.identity: tuple[int, int, int, int] | None = None
        self.version = 0
        self.failure: HistoryReadError | None = None

    @staticmethod
    def _identity(path: Path) -> tuple[int, int, int, int]:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    def sync(self) -> int:
        with self.lock:
            current = self._identity(self.path)
            if current == self.identity:
                if self.failure:
                    raise self.failure
                return self.version
            for attempt in range(2):
                before = self._identity(self.path)
                try:
                    try:
                        parser = _read_compressed_pass(self.path)
                    except _StructuredFormatDetected:
                        parser = _read_compressed_pass(self.path, force_structured=True)
                except zstd_read.CompressionError as exc:
                    failure = HistoryReadError(exc.code, exc.public_message)
                    if before == self._identity(self.path):
                        self.identity, self.failure = before, failure
                    raise failure from None
                except HistoryReadError as exc:
                    if before == self._identity(self.path):
                        self.identity, self.failure = before, exc
                    raise
                after = self._identity(self.path)
                if before == after:
                    self.parser = parser
                    self.identity = after
                    self.failure = None
                    self.version += 1
                    return self.version
            raise HistoryReadError("zstd_changed", "The compressed conversation is changing. It was not read in full; try again later.")

    def snapshot(self) -> tuple[int, list[dict[str, Any]]]:
        with self.lock:
            version = self.sync()
            return version, self.parser.snapshot()


def _is_subagent(path: Path) -> bool:
    """Exclude internal agent rollouts from the user-facing conversation list."""
    try:
        with path.open("rb") as file:
            line = file.readline(MAX_LINE + 1)
        if len(line) > MAX_LINE or not line.endswith(b"\n"):
            return False
        first = json.loads(line)
        if not isinstance(first, dict) or first.get("type") != "session_meta":
            return False
        payload = first.get("payload") or {}
        if not isinstance(payload, dict):
            return False
        source, thread_source = payload.get("source"), payload.get("thread_source")
        return ((isinstance(source, dict) and "subagent" in source)
                or source in ("subagent", "guardian_review")
                or thread_source in ("subagent", "guardian_review"))
    except (OSError, ValueError, UnicodeDecodeError, TypeError, RecursionError):
        return False


def _classify_source(source: Any, thread_source: Any = None) -> str:
    """Distinguish explicit user, internal-agent, and unconfirmed sources."""
    if isinstance(source, dict) and "subagent" in source:
        return "internal"
    if thread_source in ("subagent", "guardian_review") or source in ("subagent", "guardian_review"):
        return "internal"
    if thread_source == "user":
        return "user"
    if thread_source not in (None, ""):
        return "unknown"
    if source in ("vscode", "cli", "exec", "desktop", "app_server", "app-server", "api"):
        return "user"
    return "unknown"


def _classify_metadata_line(line: bytes) -> str:
    try:
        first = json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return "unknown"
    if not isinstance(first, dict) or first.get("type") != "session_meta":
        return "unknown"
    payload = first.get("payload") or {}
    if not isinstance(payload, dict):
        return "unknown"
    return _classify_source(payload.get("source"), payload.get("thread_source"))


def _classify_file(path: Path, compressed: bool) -> str:
    """Read only bounded session metadata, never the conversation body."""
    limit = 2 * 1024 * 1024
    try:
        if not compressed:
            with path.open("rb") as file:
                line = file.readline(limit + 1)
            return _classify_metadata_line(line) if line.endswith(b"\n") and len(line) <= limit else "unknown"
        # Avoid launching a process per unindexed file during list refreshes.
        if not zstd_read.native_available():
            return "unknown"
        pending = bytearray()
        chunks = zstd_read.iter_chunks(path, max_input_bytes=16 * 1024 * 1024,
                                       timeout_seconds=5, check_total_size=False)
        try:
            for chunk in chunks:
                pending.extend(chunk)
                end = pending.find(b"\n")
                if end >= 0:
                    return _classify_metadata_line(bytes(pending[:end])) if end <= limit else "unknown"
                if len(pending) > limit:
                    return "unknown"
        finally:
            chunks.close()
    except (OSError, zstd_read.CompressionError, RecursionError):
        return "unknown"
    return "unknown"


class UnavailableSessionReader:
    def __init__(self, path: Path, code: str, reason: str):
        self.path, self.code, self.reason = path, code, reason

    def sync(self) -> int:
        raise HistoryReadError(self.code, self.reason)

    def snapshot(self) -> tuple[int, list[dict[str, Any]]]:
        self.sync()
        return 0, []


def _state_metadata(home: Path) -> dict[str, dict[str, Any]]:
    """Read titles and user/subagent identity; never trust database paths."""
    result: dict[str, dict[str, Any]] = {}
    try:
        databases = []
        for candidate in home.glob("state_*.sqlite"):
            match = re.fullmatch(r"state_(\d+)\.sqlite", candidate.name)
            if match:
                resolved = candidate.resolve(strict=True)
                if resolved.is_relative_to(home) and resolved.is_file():
                    databases.append((int(match.group(1)), resolved))
        if not databases:
            return result
        database = max(databases, key=lambda entry: entry[0])[1]
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.5)
        try:
            connection.execute("PRAGMA query_only=ON")
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if "id" not in columns:
                return result
            select = ["id"] + [name if name in columns else "NULL" for name in ("title", "thread_source", "source")]
            for ident, title, thread_source, source in connection.execute("SELECT " + ",".join(select) + " FROM threads"):
                if not isinstance(ident, str) or not valid_thread_id(ident):
                    continue
                source_value = source
                if isinstance(source, str) and source.startswith("{"):
                    try:
                        source_value = json.loads(source)
                    except (ValueError, RecursionError):
                        pass
                classification = _classify_source(source_value, thread_source)
                result[ident.lower()] = {"classification": classification, "title": title[:300] if isinstance(title, str) else ""}
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError):
        # Source is unknown, not automatically classified as a user or an agent.
        return {}
    return result


class HistoryStore:
    def __init__(self, codex_home: Path | None = None):
        self.home = (codex_home or Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")).expanduser().resolve()
        self.roots = tuple((self.home / name).resolve() for name in ("sessions", "archived_sessions"))
        self.lock = threading.RLock()
        self.paths: dict[str, Path] = {}
        self.metadata: dict[str, dict[str, Any]] = {}
        self.readers: collections.OrderedDict[str, Any] = collections.OrderedDict()
        self._source_cache: collections.OrderedDict[str, tuple[tuple[int, int, int, int], str]] = collections.OrderedDict()
        self._last_refresh = 0.0

    def _file_source(self, path: Path, stat: Any, compressed: bool) -> str:
        signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        key = str(path)
        cached = self._source_cache.get(key)
        if cached and cached[0] == signature:
            self._source_cache.move_to_end(key)
            return cached[1]
        classification = _classify_file(path, compressed)
        self._source_cache[key] = (signature, classification)
        while len(self._source_cache) > 4096:
            self._source_cache.popitem(last=False)
        return classification

    def refresh(self) -> list[dict[str, Any]]:
        """Enumerate files under the user's Codex directories; never accept client paths."""
        with self.lock:
            database = _state_metadata(self.home)
            titles: dict[str, str] = {}
            try:
                with (self.home / "session_index.jsonl").open(encoding="utf-8") as file:
                    for line in file:
                        try:
                            row = json.loads(line)
                        except (ValueError, RecursionError):
                            continue
                        if isinstance(row, dict) and isinstance(row.get("id"), str) and isinstance(row.get("thread_name"), str):
                            titles[row["id"].lower()] = row["thread_name"][:300]
            except OSError:
                pass
            paths: dict[str, Path] = {}
            metadata: dict[str, dict[str, Any]] = {}
            for root in self.roots:
                if not root.is_dir():
                    continue
                try:
                    candidates = (file for pattern in ("*.jsonl", "*.jsonl.zst") for file in root.rglob(pattern))
                    for file in candidates:
                        match = FILENAME_ID.search(file.name)
                        if not match:
                            continue
                        ident = match.group(1).lower()
                        db_meta = database.get(ident)
                        classification = db_meta["classification"] if db_meta else "unknown"
                        if classification == "internal":
                            continue
                        compressed = file.name.endswith(".jsonl.zst")
                        try:
                            resolved = file.resolve(strict=True)
                            if not resolved.is_relative_to(root) or not resolved.is_file():
                                continue
                            stat = resolved.stat()
                            if classification == "unknown":
                                classification = self._file_source(resolved, stat, compressed)
                            if classification == "internal":
                                continue
                        except (OSError, RuntimeError):
                            continue
                        priority = (root.name == "sessions", not compressed, stat.st_mtime)
                        if ident in metadata and priority <= metadata[ident]["_priority"]:
                            continue
                        supported = (not compressed or zstd_read.available()) and classification == "user"
                        if compressed and not zstd_read.available():
                            reason, reason_code = zstd_read.availability_reason(), "zstd_unavailable"
                        elif classification != "user":
                            reason, reason_code = "The conversation source is unconfirmed. It cannot be opened to avoid exposing internal collaboration content.", "source_unconfirmed"
                        else:
                            reason, reason_code = None, None
                        paths[ident] = resolved
                        metadata[ident] = {"id": ident, "title": titles.get(ident) or (db_meta or {}).get("title") or "AI conversation", "updatedAt": _iso_time(stat.st_mtime), "source": "Codex" if classification == "user" else "Codex · Unconfirmed source", "archived": root.name == "archived_sessions", "compressed": compressed, "available": supported, "_mtime": stat.st_mtime, "_priority": priority}
                        if reason:
                            metadata[ident]["reason"] = reason
                            metadata[ident]["_reason_code"] = reason_code
                except OSError:
                    continue
            self.paths, self.metadata = paths, metadata
            self._last_refresh = time.monotonic()
            for ident, reader in list(self.readers.items()):
                if paths.get(ident) != reader.path:
                    self.readers.pop(ident, None)
            ordered = sorted(metadata.values(), key=lambda item: item["_mtime"], reverse=True)
            return [{key: value for key, value in item.items() if not key.startswith("_")} for item in ordered]

    def get(self, ident: str) -> tuple[dict[str, Any], Any]:
        ident = ident.lower()
        if not valid_thread_id(ident):
            raise KeyError("Invalid conversation")
        with self.lock:
            if ident not in self.paths or not self.paths[ident].exists():
                self.refresh()
            path = self.paths.get(ident)
            if not path:
                raise KeyError("Conversation not found")
            reader = self.readers.get(ident)
            unavailable = not self.metadata[ident]["available"]
            reason = self.metadata[ident].get("reason")
            if (reader is None or reader.path != path or isinstance(reader, UnavailableSessionReader) != unavailable
                    or (unavailable and reader.reason != reason)):
                if unavailable:
                    reader = UnavailableSessionReader(path, self.metadata[ident]["_reason_code"], reason)
                else:
                    reader = CompressedSessionReader(path) if path.name.endswith(".jsonl.zst") else SessionReader(path)
                self.readers[ident] = reader
                while len(self.readers) > 3:
                    self.readers.popitem(last=False)
            else:
                self.readers.move_to_end(ident)
            stat = path.stat()
            base = self.metadata[ident]
            metadata = {key: value for key, value in base.items() if not key.startswith("_")}
            metadata["updatedAt"] = _iso_time(stat.st_mtime)
            return metadata, reader

    def rebind(self, ident: str, reader: Any, *, force: bool = False) -> Any:
        """Switch a long-lived SSE subscriber when the preferred file changes."""
        ident = ident.lower()
        with self.lock:
            if force or time.monotonic() - self._last_refresh >= 5:
                self.refresh()
            path = self.paths.get(ident)
            if path is None:
                raise KeyError("Conversation not found")
            if path != reader.path:
                _, reader = self.get(ident)
            return reader
