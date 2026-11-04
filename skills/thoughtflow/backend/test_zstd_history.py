"""Synthetic compressed histories, decoder boundaries, and SSE rebinding."""
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import history, server, zstd_read

USER_ID = "00000000-0000-0000-0000-000000000001"
AGENT_ID = "00000000-0000-0000-0000-000000000002"
MISSING_ID = "00000000-0000-0000-0000-000000000003"
UNKNOWN_AGENT_ID = "00000000-0000-0000-0000-000000000004"
UNKNOWN_ID = "00000000-0000-0000-0000-000000000005"
NO_DB_AGENT_ID = "00000000-0000-0000-0000-000000000006"
ZSTD = shutil.which("zstd")


def event(item):
    return {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "item_completed", "turn_id": "turn-1", "item": item}}


def rollout(question="测试问题"):
    records = [
        {"type": "session_meta", "payload": {"source": "vscode"}},
        event({"type": "UserMessage", "id": "user-1", "content": [{"type": "text", "text": question}]}),
        event({"type": "Reasoning", "id": "hidden", "raw_content": "PRIVATE_REASONING_MARKER"}),
        event({"type": "AgentMessage", "id": "final", "phase": "final_answer", "content": [{"type": "Text", "text": "测试回答"}]}),
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]
    return b"".join((json.dumps(row, ensure_ascii=False) + "\n").encode() for row in records)


def compress(data):
    return subprocess.run([ZSTD, "-q", "-c"], input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout


def with_source(data, source):
    metadata = {"type": "session_meta", "payload": {"source": source}}
    return (json.dumps(metadata) + "\n").encode() + data.split(b"\n", 1)[1]


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".codex"
        self.sessions = self.home / "sessions" / "2026" / "01" / "01"
        self.archived = self.home / "archived_sessions"
        self.sessions.mkdir(parents=True)
        self.archived.mkdir(parents=True)
        connection = sqlite3.connect(str(self.home / "state_5.sqlite"))
        connection.execute("CREATE TABLE threads(id TEXT, title TEXT, thread_source TEXT, source TEXT, rollout_path TEXT)")
        connection.executemany("INSERT INTO threads VALUES(?,?,?,?,?)", [
            (USER_ID, "历史标题", "user", "vscode", str(self.sessions / f"rollout-{USER_ID}.jsonl")),
            (AGENT_ID, "子代理", "subagent", '{"subagent":{}}', str(self.sessions / f"rollout-{AGENT_ID}.jsonl")),
        ])
        connection.commit()
        connection.close()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, ident=USER_ID, *, compressed=True, archived=False):
        return (self.archived if archived else self.sessions) / (f"rollout-{ident}.jsonl" + (".zst" if compressed else ""))


class DecoderAvailabilityTests(_Fixture):
    def test_missing_decoder_is_visible_and_never_attempts_to_read(self):
        self.path().write_bytes(b"synthetic compressed placeholder")
        with patch.multiple(zstd_read, _discovered=True, _library=None, _binary=None):
            store = history.HistoryStore(self.home)
            rows = store.refresh()
            self.assertEqual(len(rows), 1)
            self.assertTrue(rows[0]["compressed"])
            self.assertFalse(rows[0]["available"])
            self.assertIn("Install zstd", rows[0]["reason"])
            with self.assertRaises(history.HistoryReadError) as caught:
                store.get(USER_ID)[1].snapshot()
            self.assertEqual(caught.exception.code, "zstd_unavailable")


@unittest.skipUnless(ZSTD and zstd_read.available(), "A Zstandard encoder and decoder are needed for compression tests")
class CompressedHistoryTests(_Fixture):
    def test_database_index_skips_agents_without_decompressing_and_parses_selected(self):
        self.path().write_bytes(compress(rollout()))
        self.path(AGENT_ID).write_bytes(b"invalid data, never opened")
        self.path(MISSING_ID).write_bytes(compress(rollout("未知来源")))
        self.path(UNKNOWN_AGENT_ID).write_bytes(compress(with_source(rollout(), {"subagent": {}})))
        self.path(NO_DB_AGENT_ID).write_bytes(compress(with_source(rollout(), {"subagent": {}})))
        self.path(UNKNOWN_ID).write_bytes(compress(with_source(rollout(), None)))
        connection = sqlite3.connect(str(self.home / "state_5.sqlite"))
        connection.execute("INSERT INTO threads VALUES(?,?,?,?,?)", (UNKNOWN_AGENT_ID, "未知来源", None, None, ""))
        connection.commit()
        connection.close()
        store = history.HistoryStore(self.home)
        with patch.object(zstd_read, "iter_chunks", wraps=zstd_read.iter_chunks) as decode:
            rows = store.refresh()
            paths = [call.args[0] for call in decode.call_args_list]
        self.assertNotIn(self.path(), paths, "known user metadata must not be decompressed for listing")
        self.assertNotIn(self.path(AGENT_ID), paths, "known internal agent must never be decompressed")
        with patch.object(zstd_read, "iter_chunks", side_effect=AssertionError("source metadata must be cached")):
            store.refresh()
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(set(by_id), {USER_ID, MISSING_ID, UNKNOWN_ID})
        self.assertEqual(by_id[USER_ID]["title"], "历史标题")
        self.assertTrue(by_id[MISSING_ID]["available"])
        self.assertIn("Unconfirmed source", by_id[UNKNOWN_ID]["source"])
        self.assertFalse(by_id[UNKNOWN_ID]["available"])
        with self.assertRaises(history.HistoryReadError) as unavailable:
            store.get(UNKNOWN_ID)[1].snapshot()
        self.assertEqual(unavailable.exception.code, "source_unconfirmed")
        metadata, reader = store.get(USER_ID)
        revision, nodes = reader.snapshot()
        self.assertEqual((metadata["compressed"], len(nodes)), (True, 1))
        self.assertEqual(nodes[0]["question"], "测试问题")
        self.assertEqual(nodes[0]["answer"], "测试回答")
        self.assertEqual(nodes[0]["state"], "completed")
        self.assertNotIn("PRIVATE_REASONING_MARKER", json.dumps(nodes))
        with patch.object(zstd_read, "iter_chunks", side_effect=AssertionError("unchanged file must use cache")):
            self.assertEqual(reader.sync(), revision)

    def test_corrupt_truncated_oversized_and_bad_records_are_explicit_failures(self):
        good = compress(rollout())
        cases = [(b"not-a-zstd-frame", "zstd_invalid"), (good[:-2], "zstd_invalid"),
                 (compress(b"not-valid-json\n"), "zstd_invalid"),
                 (compress(b"[" * 2500 + b"0" + b"]" * 2500 + b"\n"), "zstd_invalid")]
        for data, code in cases:
            with self.subTest(code=code, length=len(data)):
                self.path().write_bytes(data)
                with self.assertRaises(history.HistoryReadError) as caught:
                    history.CompressedSessionReader(self.path()).snapshot()
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(str(self.home), caught.exception.public_message)
        self.path().write_bytes(good)
        with patch.object(zstd_read, "MAX_OUTPUT_BYTES", 64):
            with self.assertRaises(history.HistoryReadError) as caught:
                history.CompressedSessionReader(self.path()).snapshot()
        self.assertEqual(caught.exception.code, "zstd_too_large")
        with patch.object(zstd_read, "MAX_INPUT_BYTES", len(good) - 1):
            with self.assertRaises(history.HistoryReadError) as caught:
                history.CompressedSessionReader(self.path()).snapshot()
        self.assertEqual(caught.exception.code, "zstd_too_large")
        with patch.object(history, "MAX_LINE", 32):
            with self.assertRaises(history.HistoryReadError) as caught:
                history.CompressedSessionReader(self.path()).snapshot()
        self.assertEqual(caught.exception.code, "record_too_large")

    def test_optional_cli_fallback_is_bounded_and_decodes(self):
        raw = rollout()
        self.path().write_bytes(compress(raw))
        with patch.multiple(zstd_read, _discovered=True, _library=None, _binary=ZSTD):
            self.assertEqual(b"".join(zstd_read.iter_chunks(self.path())), raw)
            with patch.object(zstd_read, "MAX_OUTPUT_BYTES", 16):
                with self.assertRaises(zstd_read.CompressionError) as caught:
                    b"".join(zstd_read.iter_chunks(self.path()))
            self.assertEqual(caught.exception.code, "zstd_too_large")

    def test_prefers_active_plain_file_and_rebinds_when_restored(self):
        old = self.path(archived=True)
        old.write_bytes(compress(rollout("旧归档")))
        store = history.HistoryStore(self.home)
        store.refresh()
        _, original_reader = store.get(USER_ID)
        self.assertIsInstance(original_reader, history.CompressedSessionReader)
        self.assertEqual(original_reader.snapshot()[1][0]["question"], "旧归档")
        active = self.path(compressed=False)
        active.write_bytes(rollout("恢复的活动对话"))
        # A newer compressed mtime cannot override the active plain rollout.
        os.utime(old, (active.stat().st_mtime + 20, active.stat().st_mtime + 20))
        new_reader = store.rebind(USER_ID, original_reader, force=True)
        self.assertIsInstance(new_reader, history.SessionReader)
        self.assertEqual(new_reader.snapshot()[1][0]["question"], "恢复的活动对话")
        self.assertFalse(store.get(USER_ID)[0]["compressed"])

    def test_sse_notifies_for_plain_append_and_compressed_to_plain_transition(self):
        self.path().write_bytes(compress(rollout("压缩对话")))
        store = history.HistoryStore(self.home)
        store.refresh()
        app = server.ThoughtFlowServer(0, store=store)
        port = app.server_address[1]
        thread = threading.Thread(target=app.serve_forever, daemon=True)
        thread.start()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.request("GET", f"/api/conversations/{USER_ID}/events")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn(b'"update"', response.readline())
            self.assertEqual(response.readline(), b"\n")
            active = self.path(compressed=False)
            active.write_bytes(rollout("活动对话"))
            store._last_refresh = -100
            self.assertIn(b'"update"', response.readline(), "restoring a plain log notifies the open view")
            self.assertEqual(response.readline(), b"\n")
            with active.open("ab") as file:
                file.write((json.dumps(event({"type": "UserMessage", "id": "user-2", "content": [{"type": "text", "text": "第二轮"}]}), ensure_ascii=False) + "\n").encode())
            self.assertIn(b'"update"', response.readline(), "later plain-log appends still notify")
            self.assertEqual(len(store.get(USER_ID)[1].snapshot()[1]), 2)
        finally:
            conn.close()
            app.shutdown()
            thread.join(timeout=3)
            app.server_close()


if __name__ == "__main__":
    unittest.main()
