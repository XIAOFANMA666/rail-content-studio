import http.client
import json
import os
import threading
import unittest
from unittest.mock import patch

os.environ['TEAM_ACCESS_PASSWORD'] = 'test-only-team-password'
os.environ['DEEPSEEK_API_KEY'] = 'test-only-api-key'
import review_server


class HostedServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = review_server.LimitedHTTPServer(('127.0.0.1', 0), review_server.Handler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        data = response.read()
        headers = dict(response.getheaders())
        status = response.status
        conn.close()
        return status, headers, data

    def login(self):
        body = 'password=test-only-team-password'
        status, headers, _ = self.request('POST', '/login', body, {'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/x-www-form-urlencoded'})
        self.assertEqual(status, 303)
        return headers['Set-Cookie'].split(';', 1)[0]

    def test_access_protection_and_csrf(self):
        self.assertEqual(self.request('GET', '/')[0], 303)
        self.assertEqual(self.request('POST', '/api/video-review', b'video', {'Origin': 'https://example.com', 'Content-Type': 'application/octet-stream'})[0], 403)
        cookie = self.login()
        self.assertEqual(self.request('GET', '/.env', headers={'Cookie': cookie})[0], 404)
        status, _, page = self.request('GET', '/', headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn(b'const HOSTED = true', page)

    def test_upload_is_isolated_and_cleaned(self):
        cookie = self.login()
        paths = []
        def fake_review(path, key, model):
            paths.append(path)
            with open(path, 'rb') as f:
                self.assertEqual(f.read(), b'video-test')
            return {'dimensions': [], 'limitations': ['语音在本地转成文字']}
        headers = {'Cookie': cookie, 'Origin': f'http://127.0.0.1:{self.port}', 'Content-Type': 'application/octet-stream'}
        with patch.object(review_server, 'review_video', side_effect=fake_review):
            status, _, body = self.request('POST', '/api/video-review', b'video-test', headers)
        self.assertEqual(status, 200)
        self.assertIn('服务端', json.loads(body)['review']['limitations'][0])
        self.assertFalse(os.path.exists(paths[0]))

    def test_cookie_and_model_constraints(self):
        token = review_server.make_cookie()
        self.assertTrue(review_server.authenticated('team_session=' + token))
        self.assertFalse(review_server.authenticated('team_session=' + token + 'x'))


if __name__ == '__main__':
    unittest.main()
