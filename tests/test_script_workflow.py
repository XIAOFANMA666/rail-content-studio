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

    def test_insufficient_balance_keeps_saved_scripts_and_explains_recovery(self):
        (self.out / 'script-session.json').write_text(json.dumps(self.session))
        with patch('pipeline.generate_script_session', side_effect=RuntimeError('model 返回 402：private upstream details')), patch.object(studio.llm, 'model_config', return_value={'api_key': 'test'}):
            status, data = self.post('/api/scripts', {'topic': 'test'})
        self.assertEqual(status, 400)
        self.assertIn('余额不足', data['error'])
        self.assertNotIn('private', data['error'])
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

    def editable_session(self):
        import copy
        import script_store
        from script_edit import load
        original = copy.deepcopy(load('script_candidates.json')['variants'][0])
        # Saved generated sessions have vocabulary tags; archive examples may not.
        for line in original['lines']:
            line['tags'] = ['train']
            line['places'] = []
        original['id'] = 'A'
        self.session['scripts'].update(variants=[original], generated_by='fixture')
        self.session['usage'] = {'provider': 'fixture', 'model': 'fixture'}
        return script_store.add(str(self.out), self.session)

    def save(self, session, content=None, expected=None):
        import copy
        content = copy.deepcopy(content or session['scripts']['variants'][0])
        content['lines'][0]['en'] = 'Watch the city fade as your journey begins.'
        return self.post('/api/scripts/save', {'session_id': session['id'], 'variant': 'A',
                         'content': content, 'title': '改过的选题',
                         'expected_head': expected or session['id']})

    def test_save_preserves_original_and_produces_exact_new_text(self):
        import script_store
        original = self.editable_session()
        old_line = original['scripts']['variants'][0]['lines'][0]['en']
        with patch.object(studio.llm, 'script_json') as model:
            status, saved = self.save(original)
            self.assertEqual(status, 200)
            self.assertEqual(saved['revision'], 2)
            self.assertIsNone(saved['scripts']['chosen'])
            model.assert_not_called()
        history = script_store.read(str(self.out))['sessions']
        self.assertEqual(history[0]['scripts']['variants'][0]['lines'][0]['en'], old_line)
        self.assertEqual(len(history), 2)
        self.assertEqual(studio.read_script_session()['id'], saved['id'])
        done = threading.Event(); snapshots = []
        def run(args):
            snapshots.append(json.loads(Path(args[1]).read_text())); done.set()
        with patch.object(studio, 'run_pipeline', side_effect=run):
            self.assertEqual(self.post('/api/produce', {'session_id': saved['id'], 'variant': 'A'})[0], 200)
            self.assertTrue(done.wait(2))
        self.assertEqual(snapshots[0]['id'], saved['id'])
        self.assertEqual(snapshots[0]['scripts']['variants'][0]['lines'][0]['en'], 'Watch the city fade as your journey begins.')

    def test_legacy_and_multiple_topics_survive_generation(self):
        import copy
        import script_store
        (self.out / 'script-session.json').write_text(json.dumps(self.session))
        old_bytes = (self.out / 'script-session.json').read_bytes()
        new = copy.deepcopy(self.session); new['id'] = 'other'; new['title'] = '另一个选题'
        script_store.add(str(self.out), new)
        self.assertEqual(len(script_store.summaries(str(self.out))), 2)
        self.assertEqual((self.out / 'script-session.json').read_bytes(), old_bytes)
        self.assertEqual(self.post('/api/scripts/open', {'session_id': 'new-script', 'variant': 'B'})[0], 200)
        self.assertEqual(studio.read_script_session()['scripts']['chosen'], 'B')

    def test_stale_tabs_and_invalid_content_cannot_overwrite_history(self):
        import copy
        import script_store
        original = self.editable_session()
        status, saved = self.save(original)
        self.assertEqual(status, 200)
        self.assertEqual(self.save(original)[0], 400)
        self.post('/api/scripts/open', {'session_id': original['id']})
        self.assertEqual(self.save(original)[0], 400)  # old expected head
        bad = copy.deepcopy(original['scripts']['variants'][0]); bad['lines'][0]['en'] = ''
        body = {'session_id': original['id'], 'variant': 'A', 'title': 'test', 'expected_head': saved['id'], 'content': bad}
        self.assertEqual(self.post('/api/scripts/save', body)[0], 400)
        bad['lines'][0]['en'] = 'Train'; bad['lines'][0]['tags'] = ['not-in-library']
        self.assertEqual(self.post('/api/scripts/save', body)[0], 400)
        self.assertEqual(len(script_store.summaries(str(self.out))), 2)
        self.assertFalse(studio.job['running'])

    def test_history_can_be_restored_as_new_revision(self):
        original = self.editable_session()
        _, saved = self.save(original)
        self.post('/api/scripts/open', {'session_id': original['id']})
        status, restored = self.save(original, expected=saved['id'])
        self.assertEqual(status, 200)
        self.assertEqual(restored['revision'], 3)
        self.assertEqual(restored['parent_id'], original['id'])

    def test_rewrite_previews_current_edits_without_saving(self):
        import copy
        import script_store
        original = self.editable_session()
        edited = copy.deepcopy(original['scripts']['variants'][0])
        edited['lines'][0]['en'] = 'This is my manual draft.'
        response = copy.deepcopy(edited); response['lines'][0]['en'] = 'What waits beyond the city lights?'
        with patch.object(studio.llm, 'model_config', return_value={'api_key': 'test'}), patch.object(studio.llm, 'script_json', return_value=({'variant': response}, {'model': 'test'})) as model:
            status, data = self.post('/api/scripts/rewrite', {'session_id': original['id'], 'variant': 'A',
                                    'instruction': '开头更有悬念', 'content': edited})
        self.assertEqual(status, 200)
        self.assertEqual(data['variant']['lines'][0]['en'], 'What waits beyond the city lights?')
        prompt = json.loads(model.call_args.args[1])
        self.assertEqual(prompt['原稿']['lines'][0]['en'], 'This is my manual draft.')
        self.assertEqual(len(script_store.summaries(str(self.out))), 1)
        self.assertEqual(studio.read_script_session()['id'], original['id'])
        self.assertIn('checks', data['variant'])

    def test_rewrite_failure_does_not_change_saved_content(self):
        import script_store
        original = self.editable_session()
        before = (self.out / 'script-library.json').read_bytes()
        with patch.object(studio.llm, 'model_config', return_value={'api_key': 'test'}), patch.object(studio.llm, 'script_json', return_value=({'variant': {'lines': []}}, {})):
            status, _ = self.post('/api/scripts/rewrite', {'session_id': original['id'], 'variant': 'A', 'instruction': '修改'})
        self.assertEqual(status, 400)
        self.assertEqual((self.out / 'script-library.json').read_bytes(), before)
        self.assertFalse(studio.job['running'])

    def test_atomic_write_failure_leaves_previous_library_intact(self):
        import script_store
        original = self.editable_session()
        before = (self.out / 'script-library.json').read_bytes()
        with patch.object(script_store.os, 'replace', side_effect=OSError('disk unavailable')):
            self.assertEqual(self.save(original)[0], 400)
        self.assertEqual((self.out / 'script-library.json').read_bytes(), before)
        self.assertEqual(list(self.out.glob('*.tmp')), [])

    def test_mutations_reject_cross_origin_and_malformed_requests(self):
        for path in ['/api/scripts/save', '/api/scripts/open', '/api/scripts/rewrite', '/api/scripts/import']:
            self.assertEqual(self.post(path, {}, 'https://example.org')[0], 403)
            self.assertEqual(self.post(path, ['not an object'])[0], 400)
        self.assertFalse(studio.job['running'])

    def test_saved_script_cannot_break_out_of_page_script_element(self):
        import script_store
        original = self.editable_session()
        original['title'] = '</script><script>alert(1)</script>'
        script_store.commit(str(self.out), {'current': original['id'], 'sessions': [original]})
        with patch.object(studio, 'read_result', return_value={'title': original['title']}):
            html = studio.page()
        self.assertNotIn(original['title'], html)
        self.assertIn('\\u003c/script>', html)
