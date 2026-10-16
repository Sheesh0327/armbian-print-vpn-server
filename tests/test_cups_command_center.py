import os
import sys
import unittest
import json
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from webprint.app import (
    app,
    init_db,
    get_db,
    parse_lpoptions_l,
    get_cups_overview_data,
    ACTIVE_TOKENS,
    get_setting
)

class TestCupsCommandCenter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        cls.client = app.test_client()
        init_db()

    def setUp(self):
        # Create a mock valid admin token
        self.admin_token = "test_admin_token_1234567890abcdef"
        ACTIVE_TOKENS[self.admin_token] = time.time() + 3600

    def test_parse_lpoptions_l(self):
        sample_ppd_output = """PageSize/Media Size: 2L L 3x5 4x6in 5x7in *A4 Letter Legal
MediaType/Media Type: *Plain Glossy FastGlossy
OutputMode/Print Quality: *Normal Best Photo
ColorModel/Output Mode: CMYGray KGray *RGB
Duplex/Double-Sided Printing: DuplexNoTumble DuplexTumble *None
InputSlot/Media Source: *Main
"""
        opts = parse_lpoptions_l(sample_ppd_output)
        self.assertEqual(len(opts), 6)

        # Verify PageSize
        ps = next(o for o in opts if o['name'] == 'PageSize')
        self.assertEqual(ps['label'], 'Media Size')
        self.assertEqual(ps['current'], 'A4')
        self.assertIn('A4', ps['choices'])
        self.assertIn('Letter', ps['choices'])

        # Verify ColorModel
        cm = next(o for o in opts if o['name'] == 'ColorModel')
        self.assertEqual(cm['current'], 'RGB')
        self.assertIn('KGray', cm['choices'])

        # Verify Duplex
        dp = next(o for o in opts if o['name'] == 'Duplex')
        self.assertEqual(dp['current'], 'None')
        self.assertIn('DuplexNoTumble', dp['choices'])

    def test_cups_overview_endpoint(self):
        res = self.client.get('/api/cups/overview')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('selected_printer', data)
        self.assertIn('defaults', data)
        self.assertIn('info', data)
        self.assertIn('queue', data)
        self.assertIn('ppd_options', data)

        defaults = data['defaults']
        self.assertIn('outputorder', defaults)
        self.assertIn('pagesize', defaults)
        self.assertIn('mediatype', defaults)
        self.assertIn('quality', defaults)
        self.assertIn('colormodel', defaults)
        self.assertIn('scaling', defaults)

    def test_cups_settings_unauthorized(self):
        # Sending settings without admin token must fail with 401
        res = self.client.post('/api/cups/settings', json={
            'printer': 'TestPrinter',
            'outputorder': 'reverse',
            'pagesize': 'A4'
        })
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertFalse(data.get('success'))
        self.assertIn('Unauthorized', data.get('error'))

    def test_cups_settings_authorized(self):
        # Sending settings with admin token
        res = self.client.post('/api/cups/settings', 
            headers={'X-Admin-Token': self.admin_token},
            json={
                'printer': 'HP_Smart_Tank_520',
                'outputorder': 'reverse',
                'pagesize': 'A4',
                'mediatype': 'Plain',
                'quality': 'Normal',
                'colormodel': 'RGB',
                'scaling': 'none'
            }
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertIn('successfully applied', data.get('message'))

        # Verify saved in SQLite settings
        self.assertEqual(get_setting('cups_default_outputorder'), 'reverse')
        self.assertEqual(get_setting('cups_default_pagesize'), 'A4')
        self.assertEqual(get_setting('cups_default_mediatype'), 'Plain')
        self.assertEqual(get_setting('cups_default_colormodel'), 'RGB')

    def test_cups_action_unauthorized(self):
        res = self.client.post('/api/cups/action', data={'action': 'test_page'})
        self.assertEqual(res.status_code, 401)

    def test_cups_action_invalid_action(self):
        res = self.client.post('/api/cups/action',
            headers={'X-Admin-Token': self.admin_token},
            data={'action': 'non_existent_action'}
        )
        self.assertEqual(res.status_code, 400)
        data = res.get_json()
        self.assertFalse(data.get('success'))

if __name__ == '__main__':
    unittest.main()
