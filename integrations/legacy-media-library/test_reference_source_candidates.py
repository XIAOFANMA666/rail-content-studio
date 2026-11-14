"""Reference searches preserve the shot contract without rejecting unknown visuals.

All provider data and downloaded bytes are fixtures; no model, subprocess, or
network is permitted. Source eligibility remains separate from shot verification.
"""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import commons
import shot_search


class ReferenceSourceCandidateTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript(commons.SCHEMA + '''
            CREATE TABLE asset(id TEXT PRIMARY KEY,name TEXT,extension TEXT,
                               info TEXT DEFAULT '{}',status TEXT);
            CREATE TABLE segment(id INTEGER PRIMARY KEY,asset TEXT,payload TEXT,state TEXT);
            CREATE TABLE source(id INTEGER PRIMARY KEY,asset TEXT,name TEXT,uploader TEXT,origin TEXT);
        ''')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.media = Path(self.temp.name)
        for target in ('http_transport.read', 'http_transport.subprocess.run'):
            guard = patch(target, side_effect=AssertionError('Offline test attempted network or process'))
            guard.start()
            self.addCleanup(guard.stop)
        self.fullspec = ('主体=女性；动作=手举外带咖啡、朝镜头行走；景别=中近景；'
                         '场景=火车站石墙旁；准确帧数=47；保持原速；不镜像')
        self.request = {
            'script': '女性在火车站手举咖啡，拉着行李走过；中近景跟拍',
            'required': self.fullspec, 'keywords': 'woman coffee station',
            'mode': 'agent', 'purpose': 'internal', 'infer': False,
            'videoOnly': True, 'seconds': 47 / 30,
            'request_fingerprint': 'a1' * 32,
            'reference_context': {'job_id': 'reference-job-1', 'shot_id': 's01', 'attempt': 0},
        }
        self.bytes = b'fixture-video-bytes'
        self.info = {
            'title': 'Station platform video.mp4', 'description': 'A station platform.',
            'mime': 'video/mp4', 'duration': 8, 'width': 1920, 'height': 1080,
            'license': 'CC BY 4.0', 'licenseUrl': 'https://creativecommons.org/licenses/by/4.0/',
            'author': 'Fixture author', 'size': len(self.bytes),
            'discovery': 'https://commons.wikimedia.org/wiki/File:Fixture-video.mp4',
            'mediaUrl': 'https://upload.wikimedia.org/fixture-video.mp4',
        }

    def payload(self, **changes):
        request = {**self.request, **changes}
        return commons.job_payload(request, shot_search.search(self.conn, request))

    def visual_check(self, values):
        return any(any(word in value for word in ('分镜', '画面', '视觉', '参考镜头'))
                   for value in values)

    def test_reference_contract_is_preserved_without_literal_tag_rules(self):
        result = shot_search.search(self.conn, self.request)
        self.assertFalse(any('主体=' in rule or '准确帧数=' in rule or '不镜像' in rule
                             or '朝镜头行走' in rule for rule in result['rules']))
        payload = commons.job_payload(self.request, result)
        self.assertEqual(payload['script'], self.request['script'])
        self.assertEqual(payload['fullspec'], {'script': self.request['script'], 'required': self.fullspec})
        self.assertEqual(payload['search']['required'], self.fullspec)
        self.assertEqual(payload['external_request_id'], self.request['request_fingerprint'])
        self.assertEqual(payload['reference_context'], self.request['reference_context'])
        self.assertEqual(payload['rules'], result['rules'])

    def test_reference_behavior_requires_fingerprint_and_context(self):
        for missing in ('request_fingerprint', 'reference_context'):
            request = {key: value for key, value in self.request.items() if key != missing}
            with self.subTest(missing=missing):
                result = shot_search.search(self.conn, request)
                self.assertTrue(any('主体=女性' in rule for rule in result['rules']))

    def test_unknown_visual_contract_is_a_check_and_source_file_can_download(self):
        payload = self.payload()
        exclusion, gaps, checks = commons.assess(self.info, payload)
        self.assertEqual(exclusion, '')
        self.assertEqual(gaps, [])
        self.assertTrue(self.visual_check(checks), checks)
        self.assertNotIn('核验作者和授权来源', checks)
        job_id = self.conn.execute('INSERT INTO commons_job(payload) VALUES (?)',
                                   (json.dumps(payload),)).lastrowid
        self.conn.commit()
        # This is the worker's source gate: unknown visuals stay available to
        # downstream verification, while missing authorization still holds.
        hold = '；'.join(gaps + checks) if gaps or '核验作者和授权来源' in checks else ''
        with patch.object(commons, 'evidence', return_value=dict(self.info)), \
             patch.object(commons, 'fetch', return_value=self.bytes) as fetch:
            outcome = commons.queue_candidate(self.conn, self.media, job_id,
                {'pageid': 101}, 'woman coffee station', hold_reason=hold)
        self.assertEqual(outcome, 'downloaded')
        fetch.assert_called_once()
        asset = sha256(self.bytes).hexdigest()
        self.assertEqual((self.media / asset / 'original.mp4').read_bytes(), self.bytes)
        row = self.conn.execute('SELECT asset,state FROM commons_candidate WHERE page_id=101').fetchone()
        self.assertEqual(row['asset'], asset)
        self.assertEqual(row['state'], 'hold')

    def test_explicit_media_duration_and_location_conflicts_still_exclude(self):
        cases = (
            ({'mime': 'image/jpeg'}, {}, '媒体类型不符'),
            ({'duration': 0.5}, {}, '时长不符'),
            ({'title': 'Glenfinnan station.mp4', 'description': 'Glenfinnan, Scotland.'},
             {'location': 'Euston'}, '地点不符'),
        )
        for info_changes, request_changes, reason in cases:
            with self.subTest(reason=reason):
                exclusion, _, _ = commons.assess({**self.info, **info_changes},
                                                 self.payload(**request_changes))
                self.assertIn(reason, exclusion)

    def test_missing_license_still_requires_authorization_check(self):
        exclusion, _, checks = commons.assess({**self.info, 'license': '', 'licenseUrl': ''},
                                              self.payload())
        self.assertEqual(exclusion, '')
        self.assertIn('核验作者和授权来源', checks)
        self.assertTrue(self.visual_check(checks), checks)

    def test_downloaded_metadata_does_not_satisfy_the_complete_shot(self):
        payload = self.payload()
        self.conn.execute('INSERT INTO asset(id,name,extension,info,status) VALUES (?,?,?,?,?)',
                          ('asset-1', 'fixture.mp4', '.mp4', json.dumps({'duration': 8}), 'ready'))
        self.conn.execute('INSERT INTO commons_candidate(page_id,job_id,payload,asset) VALUES (?,?,?,?)',
                          (101, 1, json.dumps(self.info), 'asset-1'))
        gaps, checks = commons.candidate_gaps(self.conn, payload, [101])
        self.assertTrue(gaps, 'Metadata-only candidate must not stop reference-shot verification')
        self.assertTrue(self.visual_check(gaps + checks), (gaps, checks))
        self.assertNotIn('仍缺可获取的视频文件', gaps)


if __name__ == '__main__':
    unittest.main()
