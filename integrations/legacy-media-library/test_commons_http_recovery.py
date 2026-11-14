"""Call POST handlers against a temporary database without sockets or workers."""
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import commons
import http_transport
import search_recovery
import server


class CommonsHTTPRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'test.sqlite3'
        with closing(self.connect()) as conn:
            conn.executescript(commons.SCHEMA)
        self.patch_db = patch.object(server, 'db', self.connect)
        self.patch_db.start()
        self.addCleanup(self.patch_db.stop)
        self.patch_wake = patch.object(server, 'commons_wake')
        self.wake = self.patch_wake.start()
        self.addCleanup(self.patch_wake.stop)
        self.patch_network = patch.object(http_transport, 'read',
                                         side_effect=AssertionError('Network is forbidden in this test'))
        self.patch_network.start()
        self.addCleanup(self.patch_network.stop)

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def post(self, path, payload):
        body = json.dumps(payload).encode()
        handler = server.Handler.__new__(server.Handler)
        handler.path = path
        handler.headers = {'Content-Type': 'application/json', 'Content-Length': str(len(body)),
                           'Host': '127.0.0.1:8765'}
        handler.rfile = BytesIO(body)
        handler.reply = Mock()
        handler.do_POST()
        handler.reply.assert_called_once()
        args = handler.reply.call_args.args
        return args[0], args[1] if len(args) > 1 else 200

    def stored(self, job_id):
        with closing(self.connect()) as conn:
            row = conn.execute('SELECT * FROM commons_job WHERE id=?', (job_id,)).fetchone()
            return row['state'], json.loads(row['payload']), json.loads(row['result'])

    def count(self):
        with closing(self.connect()) as conn:
            return conn.execute('SELECT count(*) FROM commons_job').fetchone()[0]

    def start_payload(self):
        return {'script': 'train passes a station', 'keywords': 'train station',
                'mode': 'fixed', 'videoOnly': True, 'direct': True,
                'request_fingerprint': 'AB' * 32,
                'reference_context': {'job_id': 'reference-job-1', 'shot_id': 's01', 'attempt': 0}}

    def test_start_persists_metadata_then_reuses_same_id_without_search(self):
        payload = self.start_payload()
        with patch.object(server, 'search', return_value={'matches': [], 'rules': [], 'gaps': []}) as search:
            first, status = self.post('/api/commons/start', payload)
        self.assertEqual(status, 202)
        search.assert_called_once()
        state, saved, result = self.stored(first['id'])
        self.assertEqual(state, 'queued')
        self.assertEqual(saved['external_request_id'], 'ab' * 32)
        self.assertEqual(saved['reference_context'], payload['reference_context'])
        self.assertEqual(result, {})
        with patch.object(server, 'search', side_effect=AssertionError('Must reuse before search')) as search:
            second, status = self.post('/api/commons/start', payload)
        self.assertEqual(status, 202)
        self.assertEqual(second, {'id': first['id'], 'reused': True})
        self.assertEqual(self.count(), 1)
        search.assert_not_called()

    def test_full_queue_checks_fingerprint_before_capacity_or_search(self):
        payload = self.start_payload()
        with closing(self.connect()) as conn, conn:
            existing = conn.execute('INSERT INTO commons_job(payload) VALUES (?)',
                                    (json.dumps({'external_request_id': 'ab' * 32}),)).lastrowid
            conn.executemany('INSERT INTO commons_job(payload) VALUES (?)', [('{}',)] * 4)
        with patch.object(server, 'search', side_effect=AssertionError('Full queue must not search')) as search:
            reused, status = self.post('/api/commons/start', payload)
            self.assertEqual(status, 202)
            self.assertEqual(reused, {'id': existing, 'reused': True})
            payload['request_fingerprint'] = 'cd' * 32
            blocked, status = self.post('/api/commons/start', payload)
        self.assertEqual(status, 400)
        self.assertIn('5 个任务', blocked['error'])
        self.assertEqual(self.count(), 5)
        search.assert_not_called()

    def test_legacy_429_cooldown_commits_then_expires_into_same_job(self):
        message = 'HTTP Error 429: Too Many Requests'
        counts = {'errors': [message], 'pageIds': [101], 'seen': [101], 'found': 1,
                  'stopReason': 'limited', 'rounds': [
                      {'action': 'search', 'query': 'train', 'complete': True, 'pageIds': [101]},
                      {'action': 'search', 'query': 'station', 'complete': True,
                       'pageIds': [], 'result': message}]}
        body = {'mode': 'agent', 'queries': ['train']}
        with closing(self.connect()) as conn, conn:
            job_id = conn.execute("INSERT INTO commons_job(payload,state,result) VALUES (?,'partial',?)",
                                  (json.dumps(body), json.dumps(counts))).lastrowid
        for now, remaining in ((100, 60), (140, 20)):
            with patch.object(search_recovery.time, 'time', return_value=now):
                response, status = self.post('/api/commons/resume-search', {'id': job_id})
            self.assertEqual(status, 429)
            self.assertEqual(response['id'], job_id)
            self.assertEqual(response['retry_after_seconds'], remaining)
            self.assertEqual(response['cooldown_until'], 160)
            state, _, persisted = self.stored(job_id)
            self.assertEqual(state, 'partial')
            self.assertEqual(persisted['failure']['cooldown_until'], 160)
            self.assertEqual(persisted['errors'], [message])
        self.wake.set.assert_not_called()
        with patch.object(search_recovery.time, 'time', return_value=160):
            response, status = self.post('/api/commons/resume-search', {'id': job_id})
        self.assertEqual(status, 202)
        self.assertEqual(response, {'id': job_id, 'state': 'queued', 'reused': False, 'network_retries': 1})
        self.assertEqual(self.count(), 1)
        state, _, persisted = self.stored(job_id)
        self.assertEqual(state, 'queued')
        self.assertEqual(persisted['pageIds'], [101])
        self.assertTrue(persisted['rounds'][0]['complete'])
        self.assertFalse(persisted['rounds'][1]['complete'])
        self.assertEqual(persisted['rounds'][1]['query'], 'station')
        self.assertEqual(persisted['error_history'][0]['errors'], [message])
        self.wake.set.assert_called_once()


if __name__ == '__main__':
    unittest.main()
