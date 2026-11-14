"""Integration tests: real FFmpeg/files/SQLite/HTTP; synthetic provider/model data.

These tests never call Pexels or a paid model and do not prove business accuracy.
"""
import http.client
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer

import media_library as ml
import media_http
import studio


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class MediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.temp.name) / 'fixture.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'testsrc2=size=640x360:rate=15:duration=8', '-c:v', 'libx264',
                        '-threads', '1', '-pix_fmt', 'yuv420p', str(cls.fixture)], check=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / 'output'
        (self.root / 'data').mkdir()
        (self.root / 'data/library.json').write_text('{"clips": []}')
        self.service = ml.Service(self.root, self.out)
        ml._singletons[str(self.out.resolve())] = self.service
        self.addCleanup(ml._singletons.pop, str(self.out.resolve()), None)
        self.mid = 'px999001'
        request = ml.validate_request({'query': 'test train', 'min_width': 0, 'auto_analyze': False})
        candidate = {'title': '合成测试素材', 'source_title': 'synthetic fixture', 'source': 'Pexels',
                     'source_id': '999001', 'source_url': 'https://www.pexels.com/video/999001/',
                     'download_url': 'https://videos.pexels.com/fixture.mp4', 'request': request,
                     'tags': [], 'places': [], 'issues': [], 'quality': 3, 'description': ''}
        self.tid = 'integration-task'
        with self.service.db() as db:
            db.execute('INSERT INTO media VALUES(?,?,?,?,?,?,?)',
                       (self.mid, 'Pexels', '999001', 'candidate', 1, ml.encode(candidate), ml.now()))
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?)',
                       (self.tid, 'analyze', 'running', ml.encode({'request': {'id': self.mid},
                         'candidate_ids': [], 'errors': [], 'usage': [], 'processed': 0}), ml.now()))

    def analyze(self, failure=False):
        def local_download(url, path, check):
            check()
            shutil.copyfile(self.fixture, path)

        def fixture_model(role, messages, **kwargs):
            if failure:
                raise ml.MediaError('测试：模型未配置')
            data = json.loads(messages[1]['content'][0]['text'])
            seconds = data['采样时间']
            return {'description': '合成测试图案，不是真实列车', 'tags': ['test_pattern'],
                    'suggested_places': [], 'observations': [
                        {'second': s, 'description': '测试图案'} for s in seconds],
                    'risks': [], 'match': '不符合', 'reason': '仅为集成测试',
                    'crop_risk': '未验业务适配', 'quality_notes': '合成图案'}, {'usage': {}, 'model': 'fixture'}
        with patch.object(ml, 'download', local_download), patch.object(ml, 'model_json', fixture_model):
            if failure:
                with self.assertRaises(ml.MediaError):
                    self.service.analyze(self.mid, self.tid)
            else:
                self.service.analyze(self.mid, self.tid)
        self.service.task_update(self.tid, 'done')

    def approve(self):
        m = self.service.media(self.mid)
        return self.service.decide({'id': self.mid, 'revision': m['revision'], 'action': 'approve',
             'actor': '集成测试', 'note': '测试夹具人工审批，不代表真实素材授权',
             'rights_note': '仅本次合成测试夹具', 'watched': True, 'rights_confirmed': True,
             'tags': ['test_pattern'], 'places': []})

    def test_real_decode_frames_approval_and_query(self):
        self.analyze()
        m = self.service.media(self.mid)
        self.assertEqual(m['status'], 'review')
        self.assertEqual(m['technical']['width'], 640)
        self.assertEqual(len(m['frames']), 8)
        self.assertTrue(all((self.root / f['file']).stat().st_size > 0 for f in m['frames']))
        self.assertEqual(ml.approved_clips(self.root, self.out), [])
        self.approve()
        clips = ml.approved_clips(self.root, self.out)
        self.assertEqual(clips[0]['clip_s'], 8)
        self.assertEqual(self.service.listing({'q': 'test_pattern'})['total'], 1)

    def test_model_failure_retains_real_video_and_requires_manual_basis(self):
        self.analyze(failure=True)
        m = self.service.media(self.mid)
        self.assertEqual(m['status'], 'review')
        self.assertIsNone(m['analysis'])
        self.assertEqual(len(m['frames']), 8)
        self.assertIn('未配置', m['analysis_error'])
        self.approve()
        self.assertEqual(len(ml.approved_clips(self.root, self.out)), 1)

    def test_bad_cached_download_is_discarded_for_retry(self):
        folder = self.out / 'media/candidates' / self.mid
        folder.mkdir(parents=True)
        video = folder / 'source.mp4'
        video.write_bytes(b'not-a-video')
        with self.assertRaises(ml.MediaError):
            self.service.analyze(self.mid, self.tid)
        self.assertFalse(video.exists())

    def test_search_preserves_candidates_on_partial_source_failure_and_caps_analysis(self):
        payload = {'videos': [{'id': i, 'url': 'https://www.pexels.com/video/%s/' % i,
                   'duration': 8, 'user': {'name': 'synthetic provider fixture'}, 'video_files': [
                   {'file_type': 'video/mp4', 'width': 640, 'height': 360,
                    'link': 'https://videos.pexels.com/fixture-%s.mp4' % i}]} for i in range(100, 107)]}
        request = ml.validate_request({'query': 'fixture', 'queries': ['first', 'second'],
                                       'min_width': 0, 'auto_analyze': False})
        with patch.object(ml.llm, 'load_env', return_value={'PEXELS_API_KEY': 'fixture'}), \
             patch.object(ml, 'fetch_json', side_effect=[payload, ml.MediaError('测试来源失败')]):
            self.service.search(self.tid, request)
        task = self.service.task(self.tid)
        self.assertEqual(len(task['candidate_ids']), 7)
        self.assertEqual(task['errors'][0]['query'], 'second')
        self.assertEqual(self.service.listing({'status': 'pending'})['total'], 8)
        request.update(queries=['first'], auto_analyze=True)
        with patch.object(self.service, 'analyze') as analyze:
            self.service.search(self.tid, request)
        self.assertEqual(analyze.call_count, 5)
        self.assertEqual(analyze.call_args.kwargs['request']['query'], 'fixture')

    def test_http_query_range_and_origin_protection(self):
        self.analyze()
        self.approve()
        with patch.object(studio, 'ROOT', str(self.root)), patch.object(studio, 'OUT', str(self.out)):
            server = ThreadingHTTPServer(('127.0.0.1', 0), studio.H)
            port = server.server_port
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with patch.object(studio, 'PORT', port):
                    connection = http.client.HTTPConnection('127.0.0.1', port)
                    connection.request('GET', '/api/media/list')
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(json.loads(response.read())['total'], 1)
                    clip = ml.approved_clips(self.root, self.out)[0]
                    connection.request('GET', '/' + clip['file'], headers={'Range': 'bytes=0-31'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 206)
                    self.assertEqual(len(response.read()), 32)
                    connection.request('POST', '/api/media/decide', '{}', {'Content-Type': 'application/json',
                                       'Origin': 'https://unrelated.example'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 403)
                    response.read()
                    connection.close()
            finally:
                server.shutdown()
                server.server_close()
                thread.join()

    def test_approved_clip_renders_real_mp4_without_network(self):
        import roughcut_export
        self.analyze()
        self.approve()
        clip = ml.approved_clips(self.root, self.out)[0]
        audio = self.out / 'tts'
        audio.mkdir()
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=2', str(audio / 'line1.wav')], check=True, timeout=20)
        result = {'total_s': 2, 'run_id': 'fixture', 'clips': {clip['id']: clip}, 'segments': [
            {'start': 0, 'dur': 2, 'subtitle': 'Integration fixture', 'audio': 'output/tts/line1',
             'shots': [{'clip': clip['id'], 'start': 0, 'dur': 2, 'src_in': 0}], 'stickers': []}]}
        rendered = roughcut_export.render(self.root, self.out, result)
        target = self.out / 'exports' / rendered['filename']
        specs = ml.probe(target)
        self.assertEqual((specs['width'], specs['height']), (1080, 1920))
        self.assertAlmostEqual(specs['duration'], 2, places=1)

    def test_archive_pipeline_generates_and_rewrites_real_draft(self):
        import pipeline
        repo = Path(pipeline.ROOT)
        (self.root / 'clips').symlink_to(repo / 'clips', target_is_directory=True)
        wav = self.root / 'voice.wav'
        m4a = self.root / 'voice.m4a'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                        'sine=frequency=440:duration=2', str(wav)], check=True, timeout=20)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(wav),
                        '-c:a', 'aac', str(m4a)], check=True, timeout=20)

        def fixture_tts(text, base, voice, engine):
            shutil.copyfile(wav, str(base) + '.wav')
            shutil.copyfile(m4a, str(base) + '.m4a')
            return 2, 0

        draft_root = self.root / 'drafts'
        draft_root.mkdir()
        with patch.object(pipeline, 'ROOT', str(self.root)), patch.object(pipeline, 'OUT', str(self.out)), \
             patch.object(pipeline, 'tts', fixture_tts), patch.object(pipeline, 'log_lines', []), \
             patch('sys.argv', ['pipeline.py', '--draft-root', str(draft_root)]), \
             contextlib.redirect_stdout(io.StringIO()):
            pipeline.main()
            result = json.loads((self.out / 'result.json').read_text())
            draft = Path(result['draft']['path']) / 'draft_info.json'
            self.assertTrue(draft.is_file())
            seg = result['segments'][0]
            alternate = next(c['id'] for c in seg['candidates']
                             if c['id'] != seg['shots'][0]['clip'] and not c.get('excluded'))
            swaps = self.out / 'swaps.json'
            swaps.write_text(json.dumps({'0': {'0': alternate}}))
            pipeline.redraft(str(swaps), str(draft_root))
            saved = json.loads((self.out / 'result.json').read_text())
            self.assertEqual(saved['segments'][0]['shots'][0]['clip'], alternate)
            self.assertTrue(draft.is_file())
            # A disabled new asset already in the saved timeline must survive rewriting
            # another shot, while remaining unavailable to new recall/replacement.
            self.analyze()
            approved = self.approve()
            new_clip = ml.approved_clips(self.root, self.out)[0]
            saved['clips'][self.mid] = new_clip
            saved['segments'][0]['shots'][0].update(clip=self.mid, src_in=0)
            (self.out / 'result.json').write_text(json.dumps(saved))
            self.service.decide({'id': self.mid, 'revision': approved['revision'], 'action': 'disable',
                                 'actor': '集成测试', 'note': '验证旧工程保留'})
            self.assertEqual(ml.approved_clips(self.root, self.out), [])
            second = saved['segments'][1]
            alternate = next(c['id'] for c in second['candidates']
                             if c['id'] != second['shots'][0]['clip'] and not c.get('excluded'))
            swaps.write_text(json.dumps({'1': {'0': alternate}}))
            pipeline.redraft(str(swaps), str(draft_root))
            final = json.loads((self.out / 'result.json').read_text())
            self.assertEqual(final['segments'][0]['shots'][0]['clip'], self.mid)


if __name__ == '__main__':
    unittest.main()
