import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

test_dir = tempfile.mkdtemp(prefix="test_webprint_rq_")
os.environ['WEBPRINT_DB'] = os.path.join(test_dir, 'test.db')
os.environ['WEBPRINT_SECRET_KEY'] = os.path.join(test_dir, 'secret.key')
os.environ['WEBPRINT_ROUTER_KEY'] = os.path.join(test_dir, 'router.key')
os.environ['WEBPRINT_SPOOL_PINNED'] = os.path.join(test_dir, 'pinned')
os.environ['WEBPRINT_SPOOL_HELD'] = os.path.join(test_dir, 'held')
os.environ['ROUTER_KEY'] = 'test-router-secret-key-abcdef123456'

import webprint.app as wp  # noqa: E402
from webprint.app import app, init_db, get_db  # noqa: E402

ROUTER = {'X-Router-Key': 'test-router-secret-key-abcdef123456'}
FROM_ROUTER = {'REMOTE_ADDR': '10.0.0.1'}


class FakeRun:
    def __init__(self, stdout='request id is HP-1 (1 file(s))\n'):
        self.stdout = stdout
        self.stderr = ''


class TestReviewQueue(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        app.config['TESTING'] = True
        cls.client = app.test_client()
        cls.token = 'admin-test-token'
        wp.ACTIVE_TOKENS[cls.token] = time.time() + 3600
        cls.admin = {'X-Admin-Token': cls.token}

    def setUp(self):
        conn = get_db()
        with conn:
            conn.execute("DELETE FROM router_jobs")
            conn.execute("DELETE FROM print_ledger")
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('wifi_review', 'on')")
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('wifi_max_pages', '10')")
        wp.public_print_limiter.reset()

    def make_job(self, job_id='wifi_1', pages=3, expires_in=900):
        path = os.path.join(wp.SPOOL_HELD_DIR, job_id + '.pdf')
        os.makedirs(wp.SPOOL_HELD_DIR, exist_ok=True)
        with open(path, 'wb') as f:
            f.write(b'%PDF-1.4 test')
        opts = {'printer': 'HP', 'copies': 1, 'colormodel': 'Gray', 'fitpage': True}
        conn = get_db()
        with conn:
            conn.execute(
                "INSERT INTO router_jobs (job_id, file_path, pages, copies, colormodel, printer, options_json, status,"
                " created_at, expires_at, filename) VALUES (?, ?, ?, 1, 'Gray', 'HP', ?, 'HELD', ?, ?, 'report.pdf')",
                (job_id, path, pages, json.dumps(opts), int(time.time()), int(time.time()) + expires_in))
        return path

    def post(self, url, **data):
        return self.client.post(url, data=data, headers=ROUTER, environ_base=FROM_ROUTER)

    def status(self, job_id):
        res = self.client.get('/api/router/status?job_id=' + job_id, headers=ROUTER, environ_base=FROM_ROUTER)
        return res.get_json()['status']

    def test_paid_job_waits_for_the_admin_and_prints_only_after_approval(self):
        path = self.make_job()
        self.assertEqual(self.status('wifi_1'), 'queued')
        with mock.patch.object(wp.subprocess, 'run') as run:
            res = self.post('/api/router/release', job_id='wifi_1')
            self.assertEqual(res.get_json()['status'], 'awaiting_admin')
            run.assert_not_called()
            self.assertTrue(os.path.exists(path))
            self.assertEqual(self.post('/api/router/release', job_id='wifi_1').get_json()['status'], 'awaiting_admin')

            queue = self.client.get('/api/admin/wifi/queue', headers=self.admin).get_json()
            self.assertEqual([j['job_id'] for j in queue['jobs']], ['wifi_1'])
            self.assertEqual(queue['jobs'][0]['filename'], 'report.pdf')
            prev = self.client.get('/api/admin/wifi/preview?job_id=wifi_1', headers=self.admin)
            self.assertEqual(prev.mimetype, 'application/pdf')

            run.return_value = FakeRun()
            ok = self.client.post('/api/admin/wifi/approve', data={'job_id': 'wifi_1'}, headers=self.admin)
            self.assertTrue(ok.get_json()['success'])
            self.assertEqual(run.call_count, 1)
            self.assertFalse(os.path.exists(path), 'file removed after printing')
            self.assertEqual(self.client.post('/api/admin/wifi/approve', data={'job_id': 'wifi_1'}, headers=self.admin).status_code, 409)
        conn = get_db()
        rows = conn.execute("SELECT source, status FROM print_ledger").fetchall()
        self.assertEqual([(r['source'], r['status']) for r in rows], [('wifi', 'COMPLETED')])

    def test_reject_removes_the_file_and_tells_the_router(self):
        path = self.make_job('wifi_2')
        self.post('/api/router/release', job_id='wifi_2')
        res = self.client.post('/api/admin/wifi/reject', data={'job_id': 'wifi_2', 'reason': 'unreadable'}, headers=self.admin)
        self.assertTrue(res.get_json()['success'])
        self.assertFalse(os.path.exists(path))
        status = self.client.get('/api/router/status?job_id=wifi_2', headers=ROUTER, environ_base=FROM_ROUTER).get_json()
        self.assertEqual((status['status'], status['detail']), ('rejected', 'unreadable'))

    def test_review_off_prints_immediately(self):
        self.make_job('wifi_3')
        conn = get_db()
        with conn:
            conn.execute("UPDATE settings SET value = 'off' WHERE key = 'wifi_review'")
        with mock.patch.object(wp.subprocess, 'run', return_value=FakeRun()) as run:
            res = self.post('/api/router/release', job_id='wifi_3').get_json()
        self.assertEqual(res['status'], 'printing')
        self.assertEqual(run.call_count, 1)

    def test_a_failed_print_stays_in_the_queue_so_it_can_be_retried(self):
        path = self.make_job('wifi_4')
        self.post('/api/router/release', job_id='wifi_4')
        with mock.patch.object(wp.subprocess, 'run', side_effect=RuntimeError('printer offline')):
            res = self.client.post('/api/admin/wifi/approve', data={'job_id': 'wifi_4'}, headers=self.admin)
        self.assertEqual(res.status_code, 500)
        self.assertTrue(os.path.exists(path))
        self.assertEqual(self.status('wifi_4'), 'awaiting_admin')

    def test_unreviewed_jobs_expire_and_the_router_sees_failed(self):
        path = self.make_job('wifi_5')
        self.post('/api/router/release', job_id='wifi_5')
        conn = get_db()
        with conn:
            conn.execute("UPDATE router_jobs SET review_expires_at = ? WHERE job_id = 'wifi_5'", (int(time.time()) - 1,))
        res = self.client.get('/api/router/status?job_id=wifi_5', headers=ROUTER, environ_base=FROM_ROUTER).get_json()
        self.assertEqual((res['status'], res['detail']), ('failed', 'expired'))
        self.assertFalse(os.path.exists(path))

    def test_cancel_cannot_undo_a_printed_job(self):
        self.make_job('wifi_6')
        with mock.patch.object(wp.subprocess, 'run', return_value=FakeRun()):
            self.post('/api/router/release', job_id='wifi_6')
            self.client.post('/api/admin/wifi/approve', data={'job_id': 'wifi_6'}, headers=self.admin)
        self.assertEqual(self.post('/api/router/cancel', job_id='wifi_6').status_code, 409)

    def test_admin_endpoints_need_the_admin_token(self):
        self.make_job('wifi_7')
        for method, url in (('get', '/api/admin/wifi/queue'), ('get', '/api/admin/wifi/preview?job_id=wifi_7'),
                            ('post', '/api/admin/wifi/approve'), ('post', '/api/admin/wifi/reject')):
            res = getattr(self.client, method)(url, headers=ROUTER, environ_base=FROM_ROUTER)
            self.assertEqual(res.status_code, 401, url)

    def test_inspect_obeys_the_public_print_switch_and_the_rate_limit(self):
        conn = get_db()
        with conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('public_print', 'off')")
        self.assertEqual(self.client.post('/api/inspect').status_code, 403)
        with conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('public_print', 'on')")
        codes = [self.client.post('/api/inspect').status_code for _ in range(12)]
        self.assertIn(429, codes)
        wp.public_print_limiter.reset()


if __name__ == '__main__':
    unittest.main()
