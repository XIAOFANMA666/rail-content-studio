"""Script generation must not destroy the current cut or silently choose a version."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import studio


class ScriptWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        (self.out / 'tts').mkdir()
        (self.out / 'tts/line1.m4a').write_bytes(b'existing voice')
        (self.out / 'result.json').write_text('{"title":"existing cut"}')
        self.session = {'id': 'new-script', 'title': 'test', 'country': '英国', 'platform': 'TikTok',
                        'scripts': {'variants': [{'id': 'A'}, {'id': 'B'}], 'chosen': None}}
        self.server = studio.ThreadingHTTPServer(('127.0.0.1', 0), studio.H)
        for name, value in [('OUT', str(self.out)), ('PORT', self.server.server_port),
                            ('job', {'running': False, 'log': [], 'error': None})]:
            p = patch.object(studio, name, value); p.start(); self.addCleanup(p.stop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def post(self, path, data, origin=None):
        c = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        c.request('POST', path, json.dumps(data), {'Content-Type': 'application/json',
                  'Origin': origin or f'http://localhost:{self.server.server_port}'})
        r = c.getresponse(); status, body = r.status, json.loads(r.read()); c.close()
        return status, body

    def test_generate_keeps_cut_and_audio_and_waits_for_selection(self):
        with patch('pipeline.generate_script_session', return_value=self.session), patch.object(studio.llm, 'model_config', return_value={'api_key': 'test'}):
            status, data = self.post('/api/scripts', {'topic': 'test'})
        self.assertEqual(status, 200)
        self.assertIsNone(data['scripts']['chosen'])
        self.assertEqual((self.out / 'tts/line1.m4a').read_bytes(), b'existing voice')
        self.assertEqual(json.loads((self.out / 'result.json').read_text())['title'], 'existing cut')
        self.assertEqual(studio.read_script_session()['id'], 'new-script')
        self.assertFalse(studio.job['running'])

    def test_produce_rejects_stale_or_unselected_script(self):
        (self.out / 'script-session.json').write_text(json.dumps(self.session))
        for data in [{'session_id': 'old', 'variant': 'A'}, {'session_id': 'new-script'}, {'session_id': 'new-script', 'variant': 'Z'}]:
            self.assertEqual(self.post('/api/produce', data)[0], 400)
            self.assertFalse(studio.job['running'])

    def test_selected_version_is_forwarded_without_model_call(self):
        (self.out / 'script-session.json').write_text(json.dumps(self.session))
        done = threading.Event(); called = []
        def run(args):
            called.extend(args); done.set()
        with patch.object(studio, 'run_pipeline', side_effect=run), patch('pipeline.generate_script_session') as generate:
            self.assertEqual(self.post('/api/produce', {'session_id': 'new-script', 'variant': 'B'})[0], 200)
            self.assertTrue(done.wait(2)); generate.assert_not_called()
        self.assertEqual(called[-2:], ['--variant', 'B'])

    def test_busy_and_external_origin_do_not_start_jobs(self):
        self.assertEqual(self.post('/api/scripts', {'topic': 'x'}, 'https://example.org')[0], 403)
        studio.job['running'] = True
        self.assertEqual(self.post('/api/scripts', {'topic': 'x'})[0], 409)
