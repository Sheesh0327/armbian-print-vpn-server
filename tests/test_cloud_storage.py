import os
import sys
import tempfile
import unittest
import io
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

test_dir = tempfile.mkdtemp(prefix="test_webprint_storage_")
os.environ['WEBPRINT_DB'] = os.path.join(test_dir, 'test.db')
os.environ['WEBPRINT_SECRET_KEY'] = os.path.join(test_dir, 'secret.key')
os.environ['WEBPRINT_SPOOL_PINNED'] = os.path.join(test_dir, 'pinned')
os.environ['WEBPRINT_SPOOL_INBOX'] = os.path.join(test_dir, 'inbox')
os.environ['WEBPRINT_STORAGE_PUBLIC'] = os.path.join(test_dir, 'shares', 'public')
os.environ['WEBPRINT_STORAGE_USERS'] = os.path.join(test_dir, 'shares', 'users')

from webprint.app import app, init_db, get_db, STORAGE_PUBLIC_DIR, STORAGE_USERS_DIR, STORAGE_SESSIONS  # noqa: E402


class TestCloudStorage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config['TESTING'] = True
        init_db()
        cls.client = app.test_client()

    def test_01_storage_status_unauthenticated(self):
        res = self.client.get('/api/storage/status')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertFalse(data['authenticated'])
        self.assertIn('disk', data)
        self.assertIn('total_formatted', data['disk'])

    def test_02_public_list_unauthenticated(self):
        res = self.client.get('/api/storage/list?scope=public')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['scope'], 'public')
        self.assertIn('folders', data)
        self.assertIn('files', data)

    def test_03_private_list_forbidden_without_login(self):
        res = self.client.get('/api/storage/list?scope=private')
        self.assertEqual(res.status_code, 401)
        data = res.get_json()
        self.assertFalse(data['success'])

    def test_04_login_failure_and_success(self):
        # Invalid password
        bad = self.client.post('/api/storage/login', data={'username': 'mil', 'password': 'wrongpassword'})
        self.assertEqual(bad.status_code, 401)
        self.assertFalse(bad.get_json()['success'])

        # Valid credentials for mil
        good = self.client.post('/api/storage/login', data={'username': 'mil', 'password': 'Mil@Armbian143'})
        self.assertEqual(good.status_code, 200)
        data = good.get_json()
        self.assertTrue(data['success'])
        self.assertIn('token', data)
        self.assertEqual(data['user']['username'], 'mil')

        # Test authenticated private listing with token
        token = data['token']
        priv = self.client.get('/api/storage/list?scope=private', headers={'X-Storage-Token': token})
        self.assertEqual(priv.status_code, 200)
        p_data = priv.get_json()
        self.assertTrue(p_data['success'])
        self.assertEqual(p_data['scope'], 'private')
        self.assertEqual(p_data['user']['username'], 'mil')

    def test_05_mkdir_upload_and_preview_public(self):
        # Create folder in public
        mkdir_res = self.client.post('/api/storage/mkdir', data={'scope': 'public', 'path': '', 'folder_name': 'Documents'})
        self.assertEqual(mkdir_res.status_code, 200)
        self.assertTrue(mkdir_res.get_json()['success'])

        # Upload text file into Documents
        file_content = b"Hello from Armbian Cloud Storage!"
        upload_data = {
            'scope': 'public',
            'path': 'Documents',
            'files': (io.BytesIO(file_content), 'test_note.txt')
        }
        up_res = self.client.post('/api/storage/upload', data=upload_data, content_type='multipart/form-data')
        self.assertEqual(up_res.status_code, 200)
        self.assertTrue(up_res.get_json()['success'])
        self.assertIn('test_note.txt', up_res.get_json()['uploaded'])

        # List Documents
        list_res = self.client.get('/api/storage/list?scope=public&path=Documents')
        self.assertEqual(list_res.status_code, 200)
        files = list_res.get_json()['files']
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]['name'], 'test_note.txt')
        self.assertEqual(files[0]['category'], 'text')

        # Download file
        dl_res = self.client.get('/api/storage/download?scope=public&path=Documents/test_note.txt')
        self.assertEqual(dl_res.status_code, 200)
        self.assertEqual(dl_res.data, file_content)
        dl_res.close()

        # Preview file
        prev_res = self.client.get('/api/storage/preview?scope=public&path=Documents/test_note.txt')
        self.assertEqual(prev_res.status_code, 200)
        self.assertEqual(prev_res.data, file_content)
        prev_res.close()

    def test_06_rename_and_delete(self):
        # Rename file
        ren_res = self.client.post('/api/storage/rename', data={
            'scope': 'public',
            'path': 'Documents/test_note.txt',
            'new_name': 'renamed_note.txt'
        })
        self.assertEqual(ren_res.status_code, 200)
        self.assertTrue(ren_res.get_json()['success'])

        # Verify rename
        list_res = self.client.get('/api/storage/list?scope=public&path=Documents')
        names = [f['name'] for f in list_res.get_json()['files']]
        self.assertIn('renamed_note.txt', names)
        self.assertNotIn('test_note.txt', names)

        # Delete file
        del_res = self.client.post('/api/storage/delete', data={
            'scope': 'public',
            'path': 'Documents/renamed_note.txt'
        })
        self.assertEqual(del_res.status_code, 200)
        self.assertTrue(del_res.get_json()['success'])

        # Delete folder
        del_dir = self.client.post('/api/storage/delete', data={
            'scope': 'public',
            'path': 'Documents'
        })
        self.assertEqual(del_dir.status_code, 200)
        self.assertTrue(del_dir.get_json()['success'])

    def test_07_directory_traversal_protection(self):
        # Attempt directory traversal attacks
        attacks = [
            '../../etc/passwd',
            '../..',
            'Documents/../../../shadow',
            '..\\..\\windows\\system32',
            '/etc/passwd'
        ]
        for atk in attacks:
            res = self.client.get(f'/api/storage/list?scope=public&path={atk}')
            self.assertIn(res.status_code, [400, 401, 403, 404])
            data = res.get_json()
            self.assertFalse(data['success'])

    def test_08_direct_storage_routes(self):
        for route in ['/storage', '/cloud', '/Public', '/Public/', '/public', '/public/']:
            res = self.client.get(route)
            self.assertEqual(res.status_code, 200)
            self.assertIn(b'tab-storage', res.data)
            self.assertIn(b'storage-container', res.data)
            self.assertIn(b'class="nav-btn active" data-tab="storage"', res.data)

    def test_09_logout(self):
        # Login
        login = self.client.post('/api/storage/login', data={'username': 'mil', 'password': 'Mil@Armbian143'})
        token = login.get_json()['token']

        # Logout
        logout = self.client.post('/api/storage/logout', headers={'X-Storage-Token': token})
        self.assertEqual(logout.status_code, 200)
        self.assertTrue(logout.get_json()['success'])

        # Token should now be invalid
        after = self.client.get('/api/storage/list?scope=private', headers={'X-Storage-Token': token})
        self.assertEqual(after.status_code, 401)

    def test_10_upload_hardening_and_disk_space_guard(self):
        from unittest.mock import patch

        # 1. Test insufficient disk space simulation
        with patch('shutil.disk_usage', return_value=(1000000000, 990000000, 10000000)):  # 10MB free < 50MB
            data = {
                'scope': 'public',
                'path': '',
                'files': (io.BytesIO(b"data"), 'test_full.txt')
            }
            res = self.client.post('/api/storage/upload', data=data, content_type='multipart/form-data')
            self.assertEqual(res.status_code, 507)
            self.assertFalse(res.get_json()['success'])
            self.assertIn('Insufficient server disk space', res.get_json()['error'])

        # 2. Test sanitization of nasty filenames (traversal, colons, nulls)
        upload_data = {
            'scope': 'public',
            'path': '',
            'files': (io.BytesIO(b"Safe content"), '..\\..:nasty*file?.txt')
        }
        res = self.client.post('/api/storage/upload', data=upload_data, content_type='multipart/form-data')
        self.assertEqual(res.status_code, 200)
        uploaded = res.get_json()['uploaded']
        self.assertEqual(len(uploaded), 1)
        self.assertNotIn('..', uploaded[0])
        self.assertNotIn('*', uploaded[0])
        self.assertNotIn('?', uploaded[0])

    def test_11_download_hardening_and_range_requests(self):
        # Create test file
        test_content = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        data = {
            'scope': 'public',
            'path': '',
            'files': (io.BytesIO(test_content), 'range_test.txt')
        }
        self.client.post('/api/storage/upload', data=data, content_type='multipart/form-data')

        # 1. Full download with security headers
        dl = self.client.get('/api/storage/download?scope=public&path=range_test.txt')
        self.assertEqual(dl.status_code, 200)
        self.assertEqual(dl.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertIn('private', dl.headers.get('Cache-Control', ''))
        self.assertEqual(dl.data, test_content)
        dl.close()

        # 2. HTTP Byte Range request (first 10 bytes)
        range_res = self.client.get('/api/storage/download?scope=public&path=range_test.txt', headers={'Range': 'bytes=0-9'})
        self.assertEqual(range_res.status_code, 206)
        self.assertEqual(range_res.data, b"0123456789")
        range_res.close()

        # 3. Non-existent file download returns clean 404
        dl_404 = self.client.get('/api/storage/download?scope=public&path=does_not_exist.txt')
        self.assertEqual(dl_404.status_code, 404)
        self.assertFalse(dl_404.get_json()['success'])


if __name__ == '__main__':
    unittest.main()
