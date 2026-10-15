import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

test_dir = tempfile.mkdtemp(prefix="test_webprint_trust_")
os.environ['WEBPRINT_DB'] = os.path.join(test_dir, 'test.db')
os.environ['WEBPRINT_SECRET_KEY'] = os.path.join(test_dir, 'secret.key')
os.environ['WEBPRINT_SPOOL_PINNED'] = os.path.join(test_dir, 'pinned')

import webprint.app as wp  # noqa: E402
from webprint.app import app, init_db, parse_trusted_networks, is_trusted_address  # noqa: E402


class TestTrustedDevices(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()
        app.config['TESTING'] = True
        cls.client = app.test_client()

    def get(self, url, ip):
        return self.client.get(url, environ_base={'REMOTE_ADDR': ip})

    def test_home_lan_and_modem_devices_are_allowed_by_default(self):
        for ip in ('10.0.0.112', '10.0.0.1', '192.168.1.40', '127.0.0.1'):
            self.assertEqual(self.get('/api/printers', ip).status_code, 200, ip)

    def test_guest_wifi_and_strangers_are_refused_everywhere(self):
        for ip in ('10.0.30.55', '8.8.8.8', '172.16.0.9', '192.168.2.5', '::ffff:10.0.30.7', '2001:db8::1', ''):
            for url in ('/', '/api/printers', '/api/status', '/static/pdf.min.js', '/api/reports/ledger'):
                res = self.get(url, ip or 'garbage')
                self.assertEqual(res.status_code, 403, (ip, url))
        self.assertEqual(self.client.post('/api/print', environ_base={'REMOTE_ADDR': '10.0.30.55'}).status_code, 403)
        self.assertEqual(self.client.post('/api/admin/auth', data={'password': 'x'}, environ_base={'REMOTE_ADDR': '10.0.30.55'}).status_code, 403)

    def test_ipv4_mapped_addresses_are_checked_as_ipv4(self):
        self.assertTrue(is_trusted_address('::ffff:192.168.1.5'))
        self.assertFalse(is_trusted_address('::ffff:10.0.30.5'))

    def test_the_list_can_be_narrowed_to_single_devices(self):
        nets = parse_trusted_networks('192.168.1.20, 192.168.1.21 ,nonsense,')
        self.assertTrue(is_trusted_address('192.168.1.20', nets))
        self.assertFalse(is_trusted_address('192.168.1.22', nets))
        self.assertFalse(is_trusted_address('10.0.0.112', nets))
        self.assertTrue(is_trusted_address('127.0.0.1', nets), 'the box itself is always allowed')

    def test_a_broken_list_never_widens_access(self):
        self.assertEqual(parse_trusted_networks('garbage;;'), [])
        self.assertFalse(is_trusted_address('192.168.1.20', parse_trusted_networks('garbage')))


if __name__ == '__main__':
    unittest.main()
