"""Behavioral checks on synthetic rollouts; never read a user's actual messages."""
import json
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from backend import history
from backend.history import HistoryStore, SessionReader


def event(item, turn="turn-a"):
    return {"timestamp": "2026-01-01T00:00:00Z", "type": "event_msg", "payload": {"type": "item_completed", "turn_id": turn, "item": item}}


def line(record):
    return (json.dumps(record, ensure_ascii=False) + "\n").encode()


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = self.root / "rollout-00000000-0000-0000-0000-000000000001.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_question_reply_envelope_is_readable_without_host_metadata(self):
        reply = '<send_user_message_question_reply>\n' + json.dumps([{'questionItemId': 'host-internal-id', 'question': '研究哪一款？', 'answer': '饭圈工具'}], ensure_ascii=False) + '\n</send_user_message_question_reply>'
        self.assertEqual(history._clean_question(reply), '研究哪一款？\n饭圈工具')
        literal = '这是用户引用：\n' + reply
        self.assertEqual(history._clean_question(literal), literal)

    def test_question_reply_keeps_public_pairs_for_task_grouping(self):
        reply = '<send_user_message_question_reply>\n' + json.dumps([{'questionItemId': 'HOST_CALL_SECRET', 'question': '研究哪一款？', 'answer': '饭圈工具'}], ensure_ascii=False) + '\n</send_user_message_question_reply>'
        self.session.write_bytes(b''.join(map(line, [
            event({'type': 'UserMessage', 'id': 'request', 'content': [{'type': 'text', 'text': '研究这个产品'}]}),
            event({'type': 'AgentMessage', 'phase': 'commentary', 'content': [{'type': 'text', 'text': '研究哪一款？'}]}),
            event({'type': 'UserMessage', 'id': 'reply', 'content': [{'type': 'text', 'text': reply}]}),
        ])))
        _, nodes = SessionReader(self.session).snapshot()
        self.assertEqual(nodes[1]['inputReply'], [{'question': '研究哪一款？', 'answer': '饭圈工具'}])
        self.assertNotIn('HOST_CALL_SECRET', json.dumps(nodes))
        self.assertEqual(history._question_reply('引用：' + reply), [])

    def test_public_input_tool_prompts_are_retained_without_internal_ids(self):
        request = {'type': 'response_item', 'payload': {'type': 'function_call', 'name': 'request_user_input_async', 'call_id': 'PRIVATE_CALL_ID', 'arguments': json.dumps({'questions': [{'title': '你想研究哪一款？', 'options': ['产品 A', '产品 B']}]}, ensure_ascii=False)}}
        self.session.write_bytes(b''.join(map(line, [event({'type': 'UserMessage', 'id': 'request', 'content': [{'type': 'text', 'text': '研究一个产品'}]}), request, request])))
        _, nodes = SessionReader(self.session).snapshot()
        self.assertEqual(nodes[0]['inputRequests'], ['你想研究哪一款？\n- 产品 A\n- 产品 B'])
        self.assertNotIn('PRIVATE_CALL_ID', json.dumps(nodes))

    def test_structured_incremental_read_and_private_reasoning_excluded(self):
        records = [
            event({"type": "UserMessage", "id": "user-1", "content": [{"type": "text", "text": "<in-app-browser-context source=\"ambient-ui-state\">ambient</in-app-browser-context>\n## My request:\n问题"}]}),
            event({"type": "Reasoning", "id": "hidden", "raw_content": "PRIVATE_REASONING_MARKER", "summary_text": "PRIVATE_REASONING_MARKER"}),
            event({"type": "AgentMessage", "id": "commentary", "phase": "commentary", "content": [{"type": "Text", "text": "正在处理"}]}),
            event({"type": "McpToolCall", "id": "tool-1", "server": "example", "tool": "lookup", "status": "completed", "arguments": {"query": "a"}, "result": {"content": [{"type": "text", "text": "结果"}, {"type": "image", "data": "DO_NOT_SEND_IMAGE_BYTES"}]}}),
        ]
        self.session.write_bytes(b"".join(map(line, records)))
        reader = SessionReader(self.session)
        version, nodes = reader.snapshot()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["question"], "问题")
        self.assertEqual(nodes[0]["answer"], "正在处理")
        self.assertEqual(nodes[0]["state"], "running")
        self.assertEqual(nodes[0]["tools"][0]["status"], "completed")
        self.assertNotIn("PRIVATE_REASONING_MARKER", json.dumps(nodes))
        self.assertNotIn("DO_NOT_SEND_IMAGE_BYTES", json.dumps(nodes))
        final = line(event({"type": "AgentMessage", "id": "final", "phase": "final_answer", "content": [{"type": "Text", "text": "完成"}]}))
        with self.session.open("ab") as file:
            file.write(final[:-1])
        self.assertEqual(reader.sync(), version, "incomplete JSONL is not consumed")
        with self.session.open("ab") as file:
            file.write(final[-1:])
            file.write(line({"type": "event_msg", "payload": {"type": "task_complete"}}))
        newer, nodes = reader.snapshot()
        self.assertGreater(newer, version)
        self.assertEqual(nodes[0]["answer"], "完成")
        self.assertEqual(nodes[0]["state"], "completed")
        self.assertEqual(reader.sync(), newer, "unchanged history has stable revision")

    def test_replaced_rollout_changes_revision(self):
        first = event({"type": "UserMessage", "id": "one", "content": [{"type": "text", "text": "a"}]})
        second = event({"type": "UserMessage", "id": "two", "content": [{"type": "text", "text": "b"}]})
        self.session.write_bytes(line(first))
        reader = SessionReader(self.session)
        previous, _ = reader.snapshot()
        replacement = self.root / "replacement"
        replacement.write_bytes(line(second))
        replacement.replace(self.session)
        now, nodes = reader.snapshot()
        self.assertGreater(now, previous)
        self.assertEqual(nodes[0]["question"], "b")

    def test_internal_agent_and_symlink_escape_are_not_listed(self):
        home = self.root / "home"
        sessions = home / "sessions"
        sessions.mkdir(parents=True)
        normal = sessions / "rollout-00000000-0000-0000-0000-000000000001.jsonl"
        internal = sessions / "rollout-00000000-0000-0000-0000-000000000002.jsonl"
        external = self.root / "outside.jsonl"
        normal.write_bytes(line({"type": "session_meta", "payload": {"source": "vscode"}}))
        internal.write_bytes(line({"type": "session_meta", "payload": {"source": {"subagent": {}}}}))
        external.write_bytes(line({"type": "session_meta", "payload": {"source": "vscode"}}))
        (sessions / "rollout-00000000-0000-0000-0000-000000000003.jsonl").symlink_to(external)
        store = HistoryStore(home)
        visible = store.refresh()
        self.assertEqual([r["id"] for r in visible], ["00000000-0000-0000-0000-000000000001"])
        with self.assertRaises(KeyError):
            store.get("../../outside.jsonl")

    def test_legacy_user_and_assistant_messages(self):
        self.session.write_bytes(b"".join(map(line, [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "id": "u1", "content": [{"type": "input_text", "text": "原问题"}]}},
            {"type": "event_msg", "payload": {"type": "user_message", "message": "原问题"}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": "原回答"}]}},
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ])))
        _, nodes = SessionReader(self.session).snapshot()
        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0]["answer"], "原回答")
        self.assertEqual(nodes[0]["state"], "completed")

    def test_private_phases_are_not_displayed_and_commentary_is_bounded(self):
        records = [event({"type": "UserMessage", "id": "one", "content": [{"type": "text", "text": "question"}]})]
        records.append(event({"type": "AgentMessage", "id": "private", "phase": "analysis", "content": [{"type": "Text", "text": "PRIVATE_PHASE_MARKER"}]}))
        records.extend(event({"type": "AgentMessage", "id": f"comment-{i}", "phase": "commentary", "content": [{"type": "Text", "text": f"{i}:" + "a" * 200}]}) for i in range(500))
        self.session.write_bytes(b"".join(map(line, records)))
        with patch("backend.history.MAX_ANSWER", 1000):
            _, nodes = SessionReader(self.session).snapshot()
        self.assertNotIn("PRIVATE_PHASE_MARKER", json.dumps(nodes))
        self.assertLessEqual(len(nodes[0]["answer"]), 1020)
        self.assertIn("Truncated", nodes[0]["answer"])

    def test_legacy_private_phase_is_not_displayed(self):
        self.session.write_bytes(b"".join(map(line, [
            {"type": "response_item", "payload": {"type": "message", "role": "user", "id": "u1", "content": [{"type": "input_text", "text": "问题"}]}},
            {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "analysis", "content": [{"type": "output_text", "text": "PRIVATE_PHASE_MARKER"}]}},
        ])))
        _, nodes = SessionReader(self.session).snapshot()
        self.assertNotIn("PRIVATE_PHASE_MARKER", json.dumps(nodes))
        self.assertEqual(nodes[0]["answer"], "")

    def test_host_references_are_removed_but_real_mention_and_markdown_stay(self):
        mention = "[@会议讨论](codex://threads/01234567-89ab-4cde-8fab-0123456789ab)"
        user_body = mention + "\n\n请分析这段对话。\n\n## 我自己的标题\n正文"
        host = ("## Referenced chats with Codex:\n"
                "These are live references to other chats. You MUST call read_thread first.\n"
                '[{"threadId":"01234567-89ab-4cde-8fab-0123456789ab","hostId":"local"}]\n'
                '<in-app-browser-context source="ambient-ui-state">visible tab</in-app-browser-context>\n'
                "\n## My request:\n" + user_body)
        self.session.write_bytes(line(event({"type": "UserMessage", "id": "ref", "content": [{"type": "text", "text": host}]})))
        _, nodes = SessionReader(self.session).snapshot()
        self.assertEqual(nodes[0]["question"], user_body)
        self.assertNotIn("read_thread", nodes[0]["question"])

    def test_normal_markdown_and_user_written_markup_are_not_removed(self):
        examples = [
            "## My request:\n这是我自己写的标题。",
            "## Referenced chats with Codex:\n我自己写的普通 Markdown。\n\n## My request:\n正文",
            '<in-app-browser-context>这是用户自己的内容</in-app-browser-context>',
            '普通说明\n<in-app-browser-context source="ambient-ui-state">用户引用的标签</in-app-browser-context>',
        ]
        for sample in examples:
            with self.subTest(sample=sample):
                self.assertEqual(history._clean_question(sample), sample)

    def test_steering_moves_running_state_to_new_valid_node(self):
        first = event({"type": "UserMessage", "id": "u1", "content": [{"type": "text", "text": "第一条"}]})
        self.session.write_bytes(line(first))
        reader = SessionReader(self.session)
        version, nodes = reader.snapshot()
        self.assertEqual([node["state"] for node in nodes], ["running"])
        with self.session.open("ab") as file:
            file.write(line(first))  # A duplicate record must not end the active card.
        same_version, nodes = reader.snapshot()
        self.assertEqual(same_version, version)
        self.assertEqual([node["state"] for node in nodes], ["running"])
        with self.session.open("ab") as file:
            # Structured user items have stable IDs: same words with a new ID
            # represent a real additional message, not a duplicate event.
            file.write(line(event({"type": "UserMessage", "id": "u2", "content": [{"type": "text", "text": "第一条"}]})))
            file.write(line(event({"type": "UserMessage", "id": "u3", "content": [{"type": "text", "text": "第三条"}]})))
        changed_version, nodes = reader.snapshot()
        self.assertGreater(changed_version, same_version)
        self.assertEqual([node["state"] for node in nodes], ["continued", "continued", "running"])
        self.assertEqual([node["parentId"] for node in nodes], [None, "u1", "u2"])
        with self.session.open("ab") as file:
            file.write(line({"type": "event_msg", "payload": {"type": "task_complete"}}))
        completed_version, nodes = reader.snapshot()
        self.assertGreater(completed_version, changed_version)
        self.assertEqual([node["state"] for node in nodes], ["continued", "continued", "completed"])


if __name__ == "__main__":
    unittest.main()
