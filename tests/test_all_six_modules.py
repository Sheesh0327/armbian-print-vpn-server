import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from webprint.app import (
    app, init_db,
    generate_cmyk_nozzle_check_pdf, reset_usb_printer,
    sync_cups_ledger, parse_cups_control_file,
    acquire_scanner_image
)

class TestAllSixModules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        cls.client = app.test_client()
        init_db()

    def test_module4_cmyk_nozzle_check_pdf(self):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tf:
            out_pdf = tf.name
        try:
            generate_cmyk_nozzle_check_pdf(out_pdf)
            self.assertTrue(os.path.exists(out_pdf))
            with open(out_pdf, 'rb') as f:
                pdf_bytes = f.read()
            self.assertTrue(pdf_bytes.startswith(b'%PDF-1.4'))
            self.assertTrue(b'Nozzle Check' in pdf_bytes)
            self.assertTrue(b'%%EOF' in pdf_bytes)
        finally:
            if os.path.exists(out_pdf):
                try:
                    os.remove(out_pdf)
                except Exception:
                    pass

    def test_module4_reset_usb_printer(self):
        res = reset_usb_printer()
        self.assertIsInstance(res, dict)
        self.assertTrue(res.get('success', False))

    def test_module2_page_log_parsing_and_sync(self):
        with patch('os.path.exists', return_value=True):
            mock_data = "HP_Smart_Tank_520 guest 123 [16/Oct/2026:14:00:00 +0800] 1 2 - 192.168.1.50 TestDoc.pdf A4 - duplex=none\n"
            with patch('builtins.open', unittest.mock.mock_open(read_data=mock_data)):
                synced = sync_cups_ledger()
                self.assertIsInstance(synced, int)

    def test_module6_network_services_endpoint(self):
        res = self.client.get('/api/network/services')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertIn('services', data)
        service_ids = [s['id'] for s in data['services']]
        self.assertIn('cups', service_ids)
        self.assertIn('avahi-daemon', service_ids)
        self.assertIn('smbd', service_ids)
        self.assertIn('tailscaled', service_ids)

    def test_module6_counter_sign_endpoint(self):
        res = self.client.get('/api/reports/counter-sign')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertIn('PRINT SHOP &amp; SELF-SERVICE', html)
        self.assertIn('Direct Windows PC', html)

    def test_module1_scan_status_endpoint(self):
        res = self.client.get('/api/scan/status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertIn('scanner', data)
        self.assertTrue(data['scanner']['available'])

    def test_module1_photocopy_endpoint(self):
        with patch('webprint.app.acquire_scanner_image') as mock_scan:
            mock_scan.return_value = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'
            res = self.client.post('/api/scan/copy', data={'copies': 1, 'mode': 'bw', 'scaling': 'fit'})
            self.assertEqual(res.status_code, 200)
            data = res.get_json()
            self.assertTrue(data['success'])
            self.assertIn('job_id', data)

    def test_module1_id_card_workflow(self):
        dummy_jpeg = b'\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00`\x00`\x00\x00\xff\xdb\x00C\x00\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xbf\x00\xff\xd9'
        with patch('webprint.app.acquire_scanner_image', return_value=dummy_jpeg):
            # Step 1: Scan Front
            res1 = self.client.post('/api/scan/id-card', data={'step': 'front'})
            self.assertEqual(res1.status_code, 200)
            data1 = res1.get_json()
            self.assertTrue(data1['success'])
            session_id = data1['session_id']

            # Step 2: Scan Back
            res2 = self.client.post('/api/scan/id-card', data={'step': 'back', 'session_id': session_id})
            self.assertEqual(res2.status_code, 200)
            data2 = res2.get_json()
            self.assertTrue(data2['success'])

            # Step 3: Print Composite
            res3 = self.client.post('/api/scan/id-card', data={'step': 'print', 'session_id': session_id, 'copies': 1})
            self.assertEqual(res3.status_code, 200)
            data3 = res3.get_json()
            self.assertTrue(data3['success'])

if __name__ == '__main__':
    unittest.main()
