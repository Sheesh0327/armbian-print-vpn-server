import unittest
import os
import sys

# Add parent directory to path so webprint module can be imported
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from webprint.app import (
    validate_extension,
    validate_copies,
    validate_page_range,
    validate_pin,
    ALLOWED_EXTENSIONS,
    ALLOWED_PAGESIZES,
    ALLOWED_COLORMODELS,
    ALLOWED_MEDIATYPES,
    ALLOWED_QUALITIES,
    ALLOWED_DUPLEX,
    ALLOWED_RESOLUTIONS,
    ALLOWED_MARGIN_MODES
)

class TestInputValidation(unittest.TestCase):
    def test_validate_extension_valid(self):
        valid_files = [
            'document.pdf', 'photo.png', 'image.jpg', 'image.jpeg',
            'graphic.webp', 'photo.heic', 'photo.heif', 'sample.tiff',
            'doc.docx', 'old.doc', 'sheet.xlsx', 'sheet.xls', 'notes.txt', 'data.csv'
        ]
        for f in valid_files:
            ext = validate_extension(f)
            self.assertTrue(ext in ALLOWED_EXTENSIONS)

    def test_validate_extension_invalid(self):
        invalid_files = ['script.sh', 'malware.exe', 'code.py', 'app.js', 'index.php', 'archive.zip']
        for f in invalid_files:
            with self.assertRaises(ValueError):
                validate_extension(f)

    def test_validate_copies_valid(self):
        self.assertEqual(validate_copies(1), 1)
        self.assertEqual(validate_copies(50), 50)
        self.assertEqual(validate_copies(99), 99)
        self.assertEqual(validate_copies("10"), 10)

    def test_validate_copies_invalid(self):
        invalid_copies = [0, -1, 100, 200, "abc", "", None]
        for c in invalid_copies:
            with self.assertRaises(ValueError):
                validate_copies(c)

    def test_validate_page_range_valid(self):
        self.assertEqual(validate_page_range(""), "")
        self.assertEqual(validate_page_range(None), "")
        self.assertEqual(validate_page_range("1-5"), "1-5")
        self.assertEqual(validate_page_range("1, 3, 5-10"), "1, 3, 5-10")

    def test_validate_page_range_invalid(self):
        invalid_ranges = ["1; rm -rf /", "1-5; reboot", "all", "first-three"]
        for r in invalid_ranges:
            with self.assertRaises(ValueError):
                validate_page_range(r)

    def test_validate_pin_valid(self):
        self.assertEqual(validate_pin("123456"), "123456")
        self.assertEqual(validate_pin("000000"), "000000")
        self.assertEqual(validate_pin("987654"), "987654")

    def test_validate_pin_invalid(self):
        invalid_pins = ["12345", "1234567", "12345a", "abcdef", "", None]
        for p in invalid_pins:
            with self.assertRaises(ValueError):
                validate_pin(p)

    def test_allow_lists(self):
        self.assertIn('A4', ALLOWED_PAGESIZES)
        self.assertIn('Letter', ALLOWED_PAGESIZES)
        self.assertIn('RGB', ALLOWED_COLORMODELS)
        self.assertIn('K', ALLOWED_COLORMODELS)
        self.assertIn('Plain', ALLOWED_MEDIATYPES)
        self.assertIn('Normal', ALLOWED_QUALITIES)
        self.assertIn('DuplexNoTumble', ALLOWED_DUPLEX)
        self.assertIn('300', ALLOWED_RESOLUTIONS)
        self.assertIn('printable', ALLOWED_MARGIN_MODES)

if __name__ == '__main__':
    unittest.main()
