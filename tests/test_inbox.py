import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

test_dir = tempfile.mkdtemp(prefix="test_webprint_inbox_")
os.environ['WEBPRINT_DB'] = os.path.join(test_dir, 'test.db')
os.environ['WEBPRINT_SECRET_KEY'] = os.path.join(test_dir, 'secret.key')
os.environ['WEBPRINT_SPOOL_PINNED'] = os.path.join(test_dir, 'pinned')
os.environ['WEBPRINT_SPOOL_INBOX'] = os.path.join(test_dir, 'inbox')
os.environ['WEBPRINT_SEND_KEY'] = os.path.join(test_dir, 'send.key')

KEY = 'k' * 40
import webprint.app as wp  # noqa: E402
from webprint.app import app, init_db, get_db  # noqa: E402

ROUTER = {'REMOTE_ADDR': '10.0.0.1'}
PDF = b'%PDF-1.4\n1 0 obj<<>>endobj\n'


class TestInbox(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # (the module may already have been imported by another test file, so set the paths on the module itself)
        cls.saved = (wp.SEND_KEY_PATH, wp.SPOOL_INBOX_DIR, wp.INBOX_MAX_BYTES, wp.INBOX_MAX_FILES, wp.TRUSTED_NETWORKS, wp.ROUTER_IP)
        wp.SEND_KEY_PATH = os.path.join(test_dir, 'send.key')
        wp.SPOOL_INBOX_DIR = os.path.join(test_dir, 'inbox')
        wp.INBOX_MAX_BYTES = 1024 * 1024
        wp.INBOX_MAX_FILES = 3
        wp.ROUTER_IP = '10.0.0.1'
        wp.TRUSTED_NETWORKS = wp.parse_trusted_networks('10.0.0.112')   # the router is NOT a trusted device on purpose
        with open(wp.SEND_KEY_PATH, 'w') as f:
            f.write(KEY + '\n')
        os.makedirs(wp.SPOOL_INBOX_DIR, exist_ok=True)
        init_db()
        app.config['TESTING'] = True
        cls.client = app.test_client()
        cls.token = 'admin-test-token'
        wp.ACTIVE_TOKENS[cls.token] = time.time() + 3600
        cls.admin = {'X-Admin-Token': cls.token}

    @classmethod
    def tearDownClass(cls):
        (wp.SEND_KEY_PATH, wp.SPOOL_INBOX_DIR, wp.INBOX_MAX_BYTES, wp.INBOX_MAX_FILES, wp.TRUSTED_NETWORKS, wp.ROUTER_IP) = cls.saved

    def setUp(self):
        conn = get_db()
        with conn:
            conn.execute("DELETE FROM inbox")
        for name in os.listdir(wp.SPOOL_INBOX_DIR):
            os.remove(os.path.join(wp.SPOOL_INBOX_DIR, name))

    def send(self, body=PDF, name='My%20Resume.pdf', key=KEY, ip='10.0.0.1', extra=None):
        headers = {'X-Router-Key': key, 'X-Filename': name, 'X-Device': 'aa:bb:cc:00:00:09'}
        headers.update(extra or {})
        return self.client.post('/api/inbox/upload', data=body, headers=headers, content_type='application/octet-stream',
                                environ_base={'REMOTE_ADDR': ip})

    def test_router_upload_is_stored_and_listed_for_the_admin_only(self):
        res = self.send()
        self.assertEqual(res.status_code, 200, res.get_json())
        listing = self.client.get('/api/inbox/list', headers=self.admin, environ_base={'REMOTE_ADDR': '10.0.0.112'}).get_json()
        self.assertEqual([f['filename'] for f in listing['files']], ['My Resume.pdf'])
        self.assertEqual(listing['files'][0]['device'], 'aa:bb:cc:00:00:09')
        no_login = self.client.get('/api/inbox/list', environ_base={'REMOTE_ADDR': '10.0.0.112'})
        self.assertEqual(no_login.status_code, 401)
        count = self.client.get('/api/inbox/count', environ_base={'REMOTE_ADDR': '10.0.0.112'}).get_json()
        self.assertEqual(count['count'], 1)
        got = self.client.get('/api/inbox/file?id=' + listing['files'][0]['id'], headers=self.admin,
                              environ_base={'REMOTE_ADDR': '10.0.0.112'})
        self.assertEqual(got.data, PDF)
        self.assertIn('attachment', got.headers['Content-Disposition'])

    def test_only_the_router_with_the_key_can_upload(self):
        self.assertEqual(self.send(ip='10.0.30.5').status_code, 403, 'a guest address')
        self.assertEqual(self.send(ip='10.0.0.112').status_code, 403, 'a trusted PC is not the router')
        self.assertEqual(self.send(ip='192.168.1.50').status_code, 403)
        self.assertEqual(self.send(key='wrong').status_code, 401)
        self.assertEqual(self.send(key='').status_code, 401)
        self.assertEqual(self.client.get('/api/inbox/count', environ_base={'REMOTE_ADDR': '10.0.0.1'}).status_code, 403,
                         'every other page still refuses the router when it is not a trusted device')

    def test_bad_files_are_refused(self):
        self.assertEqual(self.send(name='run.exe').status_code, 415)
        self.assertEqual(self.send(name='x.sh').status_code, 415)
        self.assertEqual(self.send(body=b'not a pdf', name='fake.pdf').status_code, 415)
        self.assertEqual(self.send(body=b'').status_code, 400)
        self.assertEqual(self.send(body=b'%PDF' + b'0' * (1024 * 1024 + 10)).status_code, 413)

    def test_odd_file_names_are_cleaned(self):
        res = self.send(name='..%2F..%2Fetc%2Fpasswd.pdf')
        self.assertEqual(res.status_code, 200)
        listing = self.client.get('/api/inbox/list', headers=self.admin, environ_base={'REMOTE_ADDR': '10.0.0.112'}).get_json()
        self.assertEqual(listing['files'][0]['filename'], 'passwd.pdf')
        files = os.listdir(wp.SPOOL_INBOX_DIR)
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].startswith('in_') and files[0].endswith('.pdf'))

    def test_the_inbox_has_a_limit(self):
        for _ in range(3):
            self.assertEqual(self.send().status_code, 200)
        self.assertEqual(self.send().status_code, 507)

    def test_delete_and_expiry(self):
        file_id = self.send().get_json()['id']
        path = [os.path.join(wp.SPOOL_INBOX_DIR, n) for n in os.listdir(wp.SPOOL_INBOX_DIR)][0]
        self.assertEqual(self.client.post('/api/inbox/delete', data={'id': file_id}, environ_base={'REMOTE_ADDR': '10.0.0.112'}).status_code, 401)
        ok = self.client.post('/api/inbox/delete', data={'id': file_id}, headers=self.admin, environ_base={'REMOTE_ADDR': '10.0.0.112'})
        self.assertTrue(ok.get_json()['success'])
        self.assertFalse(os.path.exists(path))
        self.send()
        conn = get_db()
        with conn:
            conn.execute("UPDATE inbox SET received_at = ?", (int(time.time()) - wp.INBOX_TTL_SECONDS - 5,))
        wp.cleanup_expired_jobs()
        self.assertEqual(os.listdir(wp.SPOOL_INBOX_DIR), [])

    def test_without_a_key_file_the_inbox_is_off(self):
        os.rename(wp.SEND_KEY_PATH, wp.SEND_KEY_PATH + '.off')
        try:
            self.assertEqual(self.send().status_code, 503)
        finally:
            os.rename(wp.SEND_KEY_PATH + '.off', wp.SEND_KEY_PATH)


if __name__ == '__main__':
    unittest.main()
