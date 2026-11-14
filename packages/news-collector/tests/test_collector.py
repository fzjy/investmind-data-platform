import tempfile
import unittest
import ast
from dataclasses import replace
from datetime import datetime, timezone, timedelta
from pathlib import Path

from smartparse_news import Collector, Config

NOW = datetime(2026, 11, 10, 2, tzinfo=timezone.utc)


def page(ids=(3, 2), text='【测试标题】这是正文'):
    return {'result': {'status': {'code': 0}, 'data': {'feed': {'list': [
        {'id': i, 'create_time': '2026-11-10 09:59:00', 'rich_text': text,
         'is_delete': 0} for i in ids]}}}}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(page_size=2, max_pages=1, interval_seconds=1,
                             request_interval_seconds=0, retry_count=0)
        self.path = Path(self.tmp.name) / 'state.sqlite'

    def collector(self, response):
        return Collector(self.path, self.config, fetch=lambda _: response, sleep=lambda _: None)

    def test_commit_page_and_resume_then_ack(self):
        c = self.collector(page())
        r = c.run(NOW)
        self.assertEqual(r['status'], 'catching_up')
        self.assertEqual(c.status()['nextId'], 2)
        self.assertEqual(len(c.pending()), 2)
        self.assertEqual(c.pending()[0]['record']['title'], '测试标题')
        self.assertIsNone(c.pending()[0]['record']['summary'])
        self.assertEqual(c.run(NOW)['requestCount'], 0)
        c = self.collector(page((1,)))
        self.assertEqual(c.run(NOW + timedelta(seconds=2))['status'], 'completed')
        self.assertIsNone(c.status()['nextId'])
        ids = [row['queueId'] for row in c.pending()]
        c.ack(ids)
        self.assertEqual(c.pending(), [])

    def test_failed_page_preserves_cursor_and_pending(self):
        c = self.collector(page()); c.run(NOW)
        c = self.collector({'result': {}})
        r = c.run(NOW + timedelta(seconds=2))
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(c.status()['nextId'], 2)
        self.assertEqual(len(c.pending()), 2)
        self.assertEqual(c.status()['lastError'], 'SCHEMA_CHANGED')

    def test_empty_200_fails_without_success(self):
        c = self.collector(page(())); self.assertEqual(c.run(NOW)['status'], 'failed')
        self.assertIsNone(c.status()['lastSuccessAt'])

    def test_repeat_idempotent_and_revision_queued(self):
        c = self.collector(page((1,))); c.run(NOW)
        c.ack([row['queueId'] for row in c.pending()])
        c.run(NOW + timedelta(seconds=2)); self.assertEqual(c.pending(), [])
        c = self.collector(page((1,), '【修订标题】新正文'))
        c.run(NOW + timedelta(seconds=4)); self.assertEqual(len(c.pending()), 1)
        c.ack([row['queueId'] for row in c.pending()])
        c = self.collector(page((1,)))
        c.run(NOW + timedelta(seconds=6))
        self.assertEqual(len(c.pending()), 1, 'A-B-A is a new revision')

    def test_recovery_and_same_second_items(self):
        c = self.collector(page(())); c.run(NOW)
        c = self.collector(page()); c.run(NOW + timedelta(seconds=2))
        self.assertEqual(c.status()['failures'], 0)
        self.assertEqual(len(c.pending()), 2)
        self.assertIsNotNone(c.status()['recoveredAt'])

    def test_future_timestamp_and_nonadvancing_id_rejected(self):
        c = self.collector(page()); c.run(NOW)
        self.assertEqual(c.run(NOW+timedelta(seconds=2))['status'], 'failed')
        self.assertEqual(c.status()['lastError'], 'PAGINATION_STALLED')

    def test_timeout_is_bounded_and_never_advances(self):
        calls = []
        def fetch(_):
            calls.append(1); raise TimeoutError('private diagnostic not persisted')
        c = Collector(self.path, replace(self.config, retry_count=2), fetch=fetch, sleep=lambda _: None)
        result = c.run(NOW)
        self.assertEqual(len(calls), 3)
        self.assertEqual(result['lastError'], 'SOURCE_UNAVAILABLE')
        self.assertIsNone(c.status()['nextId'])

    def test_future_invalid_and_stale(self):
        response = page((1,)); response['result']['data']['feed']['list'][0]['create_time'] = '2027-01-01 00:00:00'
        c = self.collector(response)
        self.assertEqual(c.run(NOW)['lastError'], 'FUTURE_TIMESTAMP')
        response['result']['data']['feed']['list'][0]['create_time'] = '2026-11-09 08:00:00'
        c = self.collector(response)
        self.assertEqual(c.run(NOW+timedelta(seconds=2))['lastError'], 'SOURCE_STALE')

    def test_package_has_no_host_imports(self):
        for path in (Path(__file__).parents[1]/'src').rglob('*.py'):
            for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or '').startswith(('app.', 'backend.')))

    def test_concurrent_run_is_locked(self):
        c = self.collector(page())
        db = c.connect()
        try:
            db.execute('BEGIN IMMEDIATE')
            self.assertEqual(c.run(NOW)['status'], 'locked')
        finally:
            db.close()


if __name__ == '__main__':
    unittest.main()
