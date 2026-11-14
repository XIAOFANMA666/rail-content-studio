"""Offline state-machine checks; no workers, network, or model calls."""
from email.message import Message
from email.utils import formatdate
import json
import sqlite3
import ssl
import unittest
import urllib.error

import commons
import search_recovery as recovery


class SearchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(commons.SCHEMA)
        self.addCleanup(self.conn.close)

    def put(self, *, state='partial', body=None, counts=None):
        body = body or {'mode': 'agent', 'queries': ['train filetype:video']}
        if counts is None:
            counts = {
                'found': 2, 'pageIds': [101, -202], 'seen': [101, -202],
                'errors': ['HTTP Error 429: limit'], 'stopReason': 'limited',
                'startedAt': 10, 'supplementStartedAt': 20,
                'rounds': [
                    {'action': 'search', 'query': 'train', 'complete': True, 'pageIds': [101]},
                    {'action': 'search', 'query': 'railway', 'complete': True,
                     'result': 'HTTP Error 429: limit', 'pageIds': []},
                ],
            }
            recovery.record_failure(counts, urllib.error.HTTPError('', 429, 'limit', None, None),
                                    target={'stage': 'commons', 'round_index': 1}, now=100)
        return self.conn.execute(
            'INSERT INTO commons_job(payload,state,result) VALUES (?,?,?)',
            (json.dumps(body), state, json.dumps(counts))).lastrowid

    def state(self, job_id):
        row = self.conn.execute('SELECT * FROM commons_job WHERE id=?', (job_id,)).fetchone()
        return row['state'], json.loads(row['result'])

    def test_fingerprint_is_validated_normalized_and_reuses_original_id(self):
        original = self.put(body={'external_request_id': 'ab' * 32})
        metadata = recovery.external_metadata({
            'request_fingerprint': 'AB' * 32,
            'reference_context': {'job_id': 'job-1', 'shot_id': 's01', 'attempt': 2,
                                  'irrelevant': 'must not persist'},
        })
        self.assertEqual(recovery.existing_request(self.conn, metadata), original)
        self.assertEqual(metadata['external_request_id'], 'ab' * 32)
        self.assertEqual(metadata['reference_context'],
                         {'job_id': 'job-1', 'shot_id': 's01', 'attempt': 2})
        self.assertIsNone(recovery.existing_request(self.conn, {'external_request_id': 'cd' * 32}))
        for value in ('x' * 64, 'ab' * 31, '', 123, ['a' * 64]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                recovery.external_metadata({'request_fingerprint': value})
        for context in ({'job_id': '../other'}, {'shot_id': 'x' * 81},
                        {'attempt': True}, {'attempt': 101}, 'invalid'):
            with self.subTest(context=context), self.assertRaises(ValueError):
                recovery.external_metadata({'request_fingerprint': 'a' * 64,
                                            'reference_context': context})

    def test_resume_cooldown_and_same_job_preserve_completed_rounds_and_candidates(self):
        job_id = self.put()
        with self.assertRaises(recovery.CooldownError) as error:
            recovery.resume_search(self.conn, job_id, now=120.2)
        self.assertEqual(error.exception.payload['id'], job_id)
        self.assertEqual(error.exception.payload['retry_after_seconds'], 40)
        self.assertEqual(self.state(job_id)[0], 'partial')
        response = recovery.resume_search(self.conn, job_id, now=160)
        self.assertEqual(response, {'id': job_id, 'state': 'queued', 'reused': False,
                                    'network_retries': 1})
        state, counts = self.state(job_id)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM commons_job').fetchone()[0], 1)
        self.assertEqual(state, 'queued')
        self.assertEqual(counts['pageIds'], [101, -202])
        self.assertEqual(counts['seen'], [101, -202])
        self.assertTrue(counts['rounds'][0]['complete'])
        self.assertFalse(counts['rounds'][1]['complete'])
        self.assertEqual(counts['rounds'][1]['query'], 'railway')
        self.assertEqual(counts['startedAt'], 160)
        self.assertNotIn('stopReason', counts)
        self.assertNotIn('supplementStartedAt', counts)
        self.assertEqual(counts['errors'], [])
        self.assertEqual(counts['error_history'][0]['errors'], ['HTTP Error 429: limit'])
        self.assertEqual(counts['last_failure']['category'], 'rate_limit')

    def test_queued_and_running_resume_are_idempotent_even_with_full_queue(self):
        job_id = self.put(state='queued', counts={'network_retries': 2})
        for _ in range(4):
            self.put(state='queued', counts={})
        for state in ('queued', 'running'):
            self.conn.execute('UPDATE commons_job SET state=? WHERE id=?', (state, job_id))
            response = recovery.resume_search(self.conn, job_id, now=100)
            self.assertEqual(response['state'], state)
            self.assertTrue(response['reused'])
            self.assertEqual(response['network_retries'], 2)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM commons_job').fetchone()[0], 5)

    def test_three_network_retries_are_the_limit(self):
        job_id = self.put()
        now = 1000
        for attempt in range(1, 4):
            response = recovery.resume_search(self.conn, job_id, now=now)
            self.assertEqual(response['network_retries'], attempt)
            _, counts = self.state(job_id)
            counts['errors'] = ['HTTP Error 503: unavailable']
            recovery.record_failure(counts, urllib.error.HTTPError('', 503, 'unavailable', None, None),
                                    target={'stage': 'commons', 'round_index': 1}, now=now)
            self.conn.execute("UPDATE commons_job SET state='partial',result=? WHERE id=?",
                              (json.dumps(counts), job_id))
            now += 1000
        with self.assertRaisesRegex(ValueError, '3 次'):
            recovery.resume_search(self.conn, job_id, now=now)
        self.assertEqual(self.state(job_id)[1]['network_retries'], 3)
        self.assertEqual(len(self.state(job_id)[1]['error_history']), 3)

    def test_fixed_search_resumes_failed_query_and_keeps_prior_cursor_data(self):
        counts = {'cursor': 2, 'pageIds': [101], 'seen': [101], 'errors': ['timeout']}
        recovery.record_failure(counts, TimeoutError('timeout'),
                                target={'stage': 'commons', 'query_index': 1}, now=100)
        job_id = self.put(body={'mode': 'fixed', 'queries': ['first', 'second', 'third']}, counts=counts)
        recovery.resume_search(self.conn, job_id, now=160)
        counts = self.state(job_id)[1]
        self.assertEqual(counts['cursor'], 1)
        self.assertEqual(counts['pageIds'], [101])
        self.assertEqual(counts['resume_target'], {'stage': 'commons', 'query_index': 1})

    def test_certificate_and_non_network_failures_cannot_resume(self):
        certificate = ssl.SSLCertVerificationError('certificate verify failed')
        for exc in (certificate, urllib.error.URLError(certificate),
                    urllib.error.URLError('备用 HTTPS 连接失败（CERT_HAS_EXPIRED）'),
                    urllib.error.HTTPError('', 403, 'forbidden', None, None), ValueError('bad query')):
            with self.subTest(exc=exc):
                self.assertIsNone(recovery.classification(exc))
                self.assertIsNone(recovery.record_failure({}, exc, now=100))
        job_id = self.put(counts={'errors': ['<urlopen error CERT_HAS_EXPIRED>'],
                                 'rounds': [{'action': 'search', 'complete': True,
                                             'result': '<urlopen error CERT_HAS_EXPIRED>'}]})
        with self.assertRaisesRegex(ValueError, '没有可恢复'):
            recovery.resume_search(self.conn, job_id, now=1000)
        self.assertEqual(self.state(job_id)[0], 'partial')

    def test_retry_after_date_and_exponential_backoff(self):
        now = 1800000000
        self.assertEqual(recovery.retry_after(formatdate(now+180, usegmt=True), now), 180)
        for value in (None, 'invalid', 'NaN', 'inf', '-5'):
            with self.subTest(value=value):
                self.assertEqual(recovery.retry_after(value, now), 0)
        for attempt, expected in ((0, 60), (1, 120), (2, 240), (3, 480), (4, 900), (8, 900)):
            counts = {'network_retries': attempt}
            failure = recovery.record_failure(counts, TimeoutError('timeout'), now=now)
            self.assertEqual(failure['retry_after_seconds'], expected)
        headers = Message()
        headers['Retry-After'] = formatdate(now+180, usegmt=True)
        counts = {}
        failure = recovery.record_failure(counts, urllib.error.HTTPError('', 429, 'limit', headers, None),
                                          now=now)
        self.assertEqual(failure['cooldown_until'], now+180)
        self.assertEqual(failure['retry_after_seconds'], 180)

    def test_other_job_source_cooldown_is_honored(self):
        job_id = self.put()
        counts = {'errors': []}
        headers = Message()
        headers['Retry-After'] = '300'
        recovery.record_failure(counts, urllib.error.HTTPError('', 429, 'limit', headers, None), now=100)
        self.put(counts=counts)
        with self.assertRaises(recovery.CooldownError) as error:
            recovery.resume_search(self.conn, job_id, now=200)
        self.assertEqual(error.exception.payload['cooldown_until'], 400)
        self.assertEqual(error.exception.payload['retry_after_seconds'], 200)

    def test_legacy_failure_migration_retains_original_cooldown(self):
        counts = {'errors': ['HTTP Error 429: limit'],
                  'rounds': [{'action': 'search', 'query': 'train', 'complete': True,
                              'result': 'HTTP Error 429: limit', 'pageIds': []}]}
        job_id = self.put(counts=counts)
        for now, remaining in ((100, 60), (140, 20)):
            with self.assertRaises(recovery.CooldownError) as error:
                recovery.resume_search(self.conn, job_id, now=now)
            self.conn.commit()
            self.assertEqual(error.exception.payload['retry_after_seconds'], remaining)
        recovery.resume_search(self.conn, job_id, now=160)
        self.assertEqual(self.state(job_id)[1]['rounds'][0]['query'], 'train')


if __name__ == '__main__':
    unittest.main()
