"""Synthetic organizing jobs; no model call, user history or real note writes."""
import copy
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import organize

ID = '01234567-89ab-4cde-8fab-0123456789ab'
NODES = [{'id': 'first', 'question': '研究产品😀', 'answer': '价格未披露。[来源](https://example.com)', 'state': 'completed'}]
RESULT = {'title': '产品研究', 'overview': '盈利仍待核实。', 'sections': [{'heading': '盈利情况', 'body': '价格未披露。[来源](https://example.com)', 'sources': ['T0001']}], 'openQuestions': ['价格是多少？']}


def wait_job(manager, status):
    for _ in range(200):
        job = manager.current(ID)
        if job and job['status'] == status:
            return job
        time.sleep(.01)
    raise AssertionError('job did not reach ' + status)


class OrganizeTests(unittest.TestCase):
    def test_source_key_matches_javascript_unicode_snapshot(self):
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is needed only for the cross-language snapshot check')
        output = subprocess.check_output([node, '-e', "const d=require('./web/digest.js');process.stdout.write(d.key(JSON.parse(process.argv[1])))", json.dumps(NODES, ensure_ascii=False)], cwd=organize.ROOT, text=True)
        self.assertEqual(organize.source_key(NODES), output)

    def test_valid_sources_unknown_sources_and_new_links(self):
        payload, _ = organize._input({'id': ID, 'title': '研究'}, NODES)
        clean = organize.validate_result(RESULT, payload)
        self.assertEqual(clean['sections'][0]['sourceTurnIds'], ['first'])
        self.assertIn('Source turns: 1', clean['markdown'])
        for change in [{'sources': ['T9999']}, {'body': '[新来源](https://other.invalid)'}]:
            result = copy.deepcopy(RESULT)
            result['sections'][0].update(change)
            with self.assertRaises(organize.OrganizeError):
                organize.validate_result(result, payload)

    def test_duplicate_requests_share_job_and_restore_cached_markdown(self):
        with tempfile.TemporaryDirectory() as folder:
            release = threading.Event()
            calls = []
            def runner(_):
                calls.append(True)
                release.wait(2)
                return RESULT
            manager = organize.Organizer(folder, runner)
            first = manager.start({'id': ID, 'title': '研究'}, NODES, organize.source_key(NODES))
            same = manager.start({'id': ID, 'title': '研究'}, NODES, organize.source_key(NODES))
            self.assertEqual(first['id'], same['id'])
            release.set()
            completed = wait_job(manager, 'completed')
            path = Path(folder) / (ID + '.json')
            for _ in range(100):
                if path.exists():
                    break
                time.sleep(.01)
            restored = organize.Organizer(folder, runner).current(ID)
            self.assertEqual(restored['markdown'], completed['markdown'])
            self.assertEqual(len(calls), 1)

    def test_changed_snapshot_and_large_input_are_not_silently_processed(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = organize.Organizer(folder, lambda _: RESULT)
            with self.assertRaises(organize.OrganizeError) as stale:
                manager.start({'id': ID}, NODES, '0' * 16)
            self.assertEqual(stale.exception.code, 'stale_source')
            with patch.object(organize, 'MAX_INPUT', 20):
                with self.assertRaises(organize.OrganizeError) as too_large:
                    manager.start({'id': ID}, NODES, organize.source_key(NODES))
                self.assertEqual(too_large.exception.code, 'too_large')
            self.assertEqual(manager.jobs, {})

    def test_failure_does_not_expose_raw_exception_or_fake_success(self):
        with tempfile.TemporaryDirectory() as folder:
            def fail(_):
                raise RuntimeError('PRIVATE_MARKER')
            manager = organize.Organizer(folder, fail)
            manager.start({'id': ID}, NODES, organize.source_key(NODES))
            job = wait_job(manager, 'failed')
            self.assertNotIn('PRIVATE_MARKER', json.dumps(job))
            self.assertFalse((Path(folder) / (ID + '.json')).exists())

    def test_failed_update_preserves_last_completed_draft_for_reload(self):
        with tempfile.TemporaryDirectory() as folder:
            manager = organize.Organizer(folder, lambda _: RESULT)
            manager.start({'id': ID}, NODES, organize.source_key(NODES))
            first = wait_job(manager, 'completed')
            for _ in range(100):
                if (Path(folder) / (ID + '.json')).exists():
                    break
                time.sleep(.01)
            changed = copy.deepcopy(NODES)
            changed[0]['answer'] += '新的补充。'
            def fail(_):
                raise organize.OrganizeError('timeout', '本次整理超时')
            manager.runner = fail
            manager.start({'id': ID}, changed, organize.source_key(changed))
            latest = wait_job(manager, 'failed')
            self.assertEqual(latest['lastCompleted']['markdown'], first['markdown'])


if __name__ == '__main__':
    unittest.main()
