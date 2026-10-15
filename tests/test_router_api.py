import unittest
import os
import sys
import tempfile
import io

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Configure isolated environment for testing
test_dir = tempfile.mkdtemp(prefix="test_webprint_")
os.environ['WEBPRINT_DB'] = os.path.join(test_dir, 'test.db')
os.environ['WEBPRINT_SECRET_KEY'] = os.path.join(test_dir, 'secret.key')
os.environ['WEBPRINT_ROUTER_KEY'] = os.path.join(test_dir, 'router.key')
os.environ['WEBPRINT_SPOOL_PINNED'] = os.path.join(test_dir, 'pinned')
os.environ['WEBPRINT_SPOOL_HELD'] = os.path.join(test_dir, 'held')
os.environ['ROUTER_KEY'] = 'test-router-secret-key-abcdef123456'

from webprint.app import app, init_db, get_router_key

class TestRouterAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        app.config['TESTING'] = True
        cls.client = app.test_client()
        cls.router_key = get_router_key()

    def test_router_missing_key_header(self):
        res = self.client.get('/api/router/health', environ_base={'REMOTE_ADDR': '10.0.0.1'})
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertFalse(data['success'])
        self.assertIn('missing X-Router-Key', data['error'])

    def test_router_invalid_key_header(self):
        res = self.client.get(
            '/api/router/health',
            headers={'X-Router-Key': 'wrong-key'},
            environ_base={'REMOTE_ADDR': '10.0.0.1'}
        )
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertFalse(data['success'])
        self.assertIn('invalid router key', data['error'])

    def test_router_unauthorized_ip(self):
        res = self.client.get(
            '/api/router/health',
            headers={'X-Router-Key': self.router_key},
            environ_base={'REMOTE_ADDR': '192.168.1.100'}
        )
        self.assertEqual(res.status_code, 403)
        data = res.get_json()
        self.assertFalse(data['success'])
        self.assertIn('source IP not authorized', data['error'])

    def test_router_health_authorized(self):
        res = self.client.get(
            '/api/router/health',
            headers={'X-Router-Key': self.router_key},
            environ_base={'REMOTE_ADDR': '10.0.0.1'}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertIn('overall', data)
        self.assertIn('printers', data)

    def test_router_job_submission_and_cancel(self):
        # Create dummy PDF file
        pdf_data = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
        data = {
            'file': (io.BytesIO(pdf_data), 'test_wifi_doc.pdf'),
            'copies': '2',
            'colormodel': 'RGB'
        }
        res = self.client.post(
            '/api/router/job',
            data=data,
            content_type='multipart/form-data',
            headers={'X-Router-Key': self.router_key},
            environ_base={'REMOTE_ADDR': '10.0.0.1'}
        )
        self.assertEqual(res.status_code, 200)
        resp_data = res.get_json()
        self.assertTrue(resp_data['success'])
        self.assertTrue(resp_data['job_id'].startswith('wifi_'))
        self.assertTrue(resp_data['color'])
        self.assertGreater(resp_data['expires_at'], 0)

        job_id = resp_data['job_id']

        # Check status
        status_res = self.client.get(
            f'/api/router/status?job_id={job_id}',
            headers={'X-Router-Key': self.router_key},
            environ_base={'REMOTE_ADDR': '10.0.0.1'}
        )
        self.assertEqual(status_res.status_code, 200)
        status_data = status_res.get_json()
        self.assertEqual(status_data['status'], 'queued')

        # Cancel job
        cancel_res = self.client.post(
            '/api/router/cancel',
            json={'job_id': job_id},
            headers={'X-Router-Key': self.router_key},
            environ_base={'REMOTE_ADDR': '10.0.0.1'}
        )
        self.assertEqual(cancel_res.status_code, 200)
        cancel_data = cancel_res.get_json()
        self.assertTrue(cancel_data['success'])

if __name__ == '__main__':
    unittest.main()
