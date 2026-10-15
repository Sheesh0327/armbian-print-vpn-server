import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

test_dir = tempfile.mkdtemp(prefix="test_webprint_js_")
os.environ['WEBPRINT_DB'] = os.path.join(test_dir, 'test.db')
os.environ['WEBPRINT_SECRET_KEY'] = os.path.join(test_dir, 'secret.key')
os.environ['WEBPRINT_SPOOL_PINNED'] = os.path.join(test_dir, 'pinned')

from webprint.app import INDEX_HTML  # noqa: E402


@unittest.skipUnless(shutil.which('node'), 'node is not installed')
class TestPageScript(unittest.TestCase):
    def test_the_inline_page_script_parses(self):
        """One stray quote inside the Python string once broke every button on the page."""
        scripts = re.findall(r'<script>(.*?)</script>', INDEX_HTML, re.S)
        self.assertTrue(scripts)
        for i, code in enumerate(scripts):
            path = os.path.join(test_dir, f'page{i}.js')
            with open(path, 'w', encoding='utf-8') as f:
                f.write(code)
            res = subprocess.run(['node', '--check', path], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, res.stderr[:600])


if __name__ == '__main__':
    unittest.main()
