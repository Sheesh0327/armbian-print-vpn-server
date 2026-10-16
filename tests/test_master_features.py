import os
import sys
import unittest
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from webprint.app import (
    app,
    init_db,
    parse_page_ranges,
    build_lp_command,
    SPOOL_PREVIEWS_DIR,
    CANONICAL_PAGESIZES,
    CANONICAL_MEDIATYPES,
    ALLOWED_COLORMODELS,
    ALLOWED_PAGESIZES
)

class TestMasterFeatures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        cls.client = app.test_client()
        os.makedirs(SPOOL_PREVIEWS_DIR, exist_ok=True)
        init_db()

    def test_parse_page_ranges(self):
        # Empty or None selects all pages
        self.assertEqual(parse_page_ranges("", 10), 10)
        self.assertEqual(parse_page_ranges(None, 5), 5)

        # Simple ranges and lists
        self.assertEqual(parse_page_ranges("1-3", 10), 3)
        self.assertEqual(parse_page_ranges("1, 3, 5", 10), 3)
        self.assertEqual(parse_page_ranges("1-3, 5, 7-8", 10), 6)

        # Clamping and deduplication
        self.assertEqual(parse_page_ranges("1-5, 3-6", 10), 6) # pages 1,2,3,4,5,6
        self.assertEqual(parse_page_ranges("8-15", 10), 3) # pages 8,9,10
        self.assertEqual(parse_page_ranges("20-30", 10), 10) # out of range defaults to total_pages

    def test_build_lp_command_translations(self):
        # 4x6in translates to canonical 4x6
        cmd = build_lp_command('HP_Test', '/tmp/doc.pdf', {'pagesize': '4x6in'})
        self.assertIn('PageSize=4x6', cmd)

        # EnvelopeDL translates to Envelope_DL
        cmd = build_lp_command('HP_Test', '/tmp/doc.pdf', {'pagesize': 'EnvelopeDL'})
        self.assertIn('PageSize=Envelope_DL', cmd)

        # FastGlossy translates to Glossy
        cmd = build_lp_command('HP_Test', '/tmp/doc.pdf', {'mediatype': 'FastGlossy'})
        self.assertIn('MediaType=Glossy', cmd)

        # KGray emits monochrome flags
        cmd = build_lp_command('HP_Test', '/tmp/doc.pdf', {'colormodel': 'KGray'})
        self.assertIn('ColorModel=KGray', cmd)
        self.assertIn('print-color-mode=monochrome', cmd)

        # RGB emits color flags
        cmd = build_lp_command('HP_Test', '/tmp/doc.pdf', {'colormodel': 'RGB'})
        self.assertIn('ColorModel=RGB', cmd)
        self.assertIn('print-color-mode=color', cmd)

        # fitpage False emits print-scaling=none for 100% millimeter accuracy
        cmd = build_lp_command('HP_Test', '/tmp/doc.pdf', {'fitpage': False})
        self.assertIn('print-scaling=none', cmd)

        # fitpage True emits print-scaling=fit
        cmd = build_lp_command('HP_Test', '/tmp/doc.pdf', {'fitpage': True})
        self.assertIn('print-scaling=fit', cmd)

    def test_preview_endpoint(self):
        # Invalid preview token regex rejected
        res = self.client.get('/api/preview/invalid_token.pdf')
        self.assertEqual(res.status_code, 400)

        # Valid format but non-existent file
        res = self.client.get('/api/preview/prev_12345_abcdef.pdf')
        self.assertEqual(res.status_code, 404)

        # Existing cached preview file
        test_prev_id = f"prev_{int(time.time())}_1234abcd"
        fpath = os.path.join(SPOOL_PREVIEWS_DIR, f"{test_prev_id}.pdf")
        with open(fpath, 'wb') as f:
            f.write(b'%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>startxref\n0\n%%EOF')
        try:
            res = self.client.get(f'/api/preview/{test_prev_id}.pdf')
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.content_type, 'application/pdf')
            self.assertTrue(res.data.startswith(b'%PDF'))
        finally:
            if 'res' in locals() and hasattr(res, 'close'):
                res.close()
            if os.path.exists(fpath):
                os.remove(fpath)

    def test_job_status_endpoint(self):
        res = self.client.get('/api/jobs/HP_Smart_Tank_520-999/status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertIn('status', data)

if __name__ == '__main__':
    unittest.main()
