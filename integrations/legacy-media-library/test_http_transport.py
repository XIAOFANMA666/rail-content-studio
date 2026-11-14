"""Offline checks for the shared transport and real Node stream enforcement."""
import json
from email.message import Message
from io import BytesIO
import ssl
import subprocess
import time
import unittest
from unittest.mock import patch
import urllib.error

import commons
import http_transport as transport
import search_agent
import video_sources


OK = {'status': 200, 'type': 'image/jpeg', 'location': ''}
EOF = urllib.error.URLError(ssl.SSLEOFError('TLS EOF'))


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.node = patch.object(transport, 'node_binary', return_value='/node')
        self.node.start()
        self.addCleanup(self.node.stop)

    def test_tls_eof_uses_node_once(self):
        with patch.object(transport, '_urllib_once', side_effect=EOF) as first, \
             patch.object(transport, '_node_once', return_value=(OK, b'photo')) as fallback:
            self.assertEqual(commons.fetch('https://upload.wikimedia.org/a.jpg', 12, 'image/jpeg'), b'photo')
            self.assertEqual(first.call_count, 1)
            self.assertEqual(fallback.call_count, 1)

    def test_certificate_failure_is_not_retried(self):
        exc = urllib.error.URLError(ssl.SSLCertVerificationError('invalid certificate'))
        with patch.object(transport, '_urllib_once', side_effect=exc), \
             patch.object(transport, '_node_once') as fallback:
            with self.assertRaises(urllib.error.URLError):
                commons.fetch('https://commons.wikimedia.org/a', 100)
            fallback.assert_not_called()

    def test_http_error_does_not_retry(self):
        with patch.object(transport, '_urllib_once', return_value=(
                {'status':429, 'type':'', 'location':'', 'retry_after':'120'}, b'')), \
             patch.object(transport, '_node_once') as fallback:
            with self.assertRaises(urllib.error.HTTPError) as error:
                commons.fetch('https://commons.wikimedia.org/a', 100)
            self.assertEqual(error.exception.code, 429)
            self.assertEqual(error.exception.headers['Retry-After'], '120')
            fallback.assert_not_called()

    def test_urllib_http_error_preserves_retry_after_date(self):
        headers = Message()
        headers['Retry-After'] = 'Sun, 15 Nov 2026 12:00:00 GMT'
        upstream = urllib.error.HTTPError('https://commons.wikimedia.org/a', 503,
                                         'unavailable', headers, BytesIO())
        with patch.object(transport.urllib.request, 'build_opener') as build:
            build.return_value.open.side_effect = upstream
            meta, body = transport._urllib_once('https://commons.wikimedia.org/a', {},
                                               None, 100, None, time.monotonic()+5)
        self.assertEqual(meta['retry_after'], headers['Retry-After'])
        self.assertEqual(meta['status'], 503)
        self.assertEqual(body, b'')

    def test_node_metadata_preserves_retry_after_without_running_node(self):
        meta = {'status':503, 'type':'text/plain', 'location':'', 'retry_after':'90'}
        done = subprocess.CompletedProcess([], 0, json.dumps(meta).encode()+b'\n')
        with patch.object(transport.subprocess, 'run', return_value=done) as run, \
             patch.object(transport, '_urllib_once', side_effect=EOF):
            with self.assertRaises(urllib.error.HTTPError) as error:
                commons.fetch('https://commons.wikimedia.org/a', 100)
        self.assertEqual(error.exception.headers['Retry-After'], '90')
        self.assertEqual(run.call_count, 1)
        self.assertIn("response.headers.get('retry-after')", run.call_args.args[0][-1])

    def test_mime_or_size_errors_do_not_retry(self):
        for message in ('mime', 'limit'):
            with patch.object(transport, '_urllib_once', side_effect=ValueError(message)), \
                 patch.object(transport, '_node_once') as fallback:
                with self.assertRaises(ValueError):
                    commons.fetch('https://commons.wikimedia.org/a', 100)
                fallback.assert_not_called()

    def test_offlist_redirect_blocked_before_second_connection(self):
        for reader,url in ((commons.fetch,'https://commons.wikimedia.org/a'),
                           (video_sources.archive_read,'https://archive.org/a')):
            with patch.object(transport, '_urllib_once', side_effect=EOF), \
                 patch.object(transport, '_node_once', return_value=(
                     {'status':302, 'type':'', 'location':'https://127.0.0.1/private'}, b'')) as fallback:
                with self.assertRaises(ValueError):
                    reader(url, 100)
                self.assertEqual(fallback.call_count, 1)

    def test_permitted_redirect_and_relative_redirect(self):
        redirects = [({'status':302, 'type':'', 'location':'https://upload.wikimedia.org/b'}, b''),
                     ({'status':301, 'type':'', 'location':'/c'}, b''), (OK,b'asset')]
        with patch.object(transport, '_urllib_once', side_effect=EOF), \
             patch.object(transport, '_node_once', side_effect=redirects) as fallback:
            self.assertEqual(commons.fetch('https://commons.wikimedia.org/a', 100), b'asset')
            self.assertEqual([call.args[0] for call in fallback.call_args_list],
                             ['https://commons.wikimedia.org/a','https://upload.wikimedia.org/b','https://upload.wikimedia.org/c'])

    def test_redirect_loop_is_bounded(self):
        with patch.object(transport, '_urllib_once', return_value=(
                {'status':302, 'type':'', 'location':'/again'}, b'')) as first:
            with self.assertRaisesRegex(ValueError, '重定向'):
                commons.fetch('https://commons.wikimedia.org/a', 100)
            self.assertEqual(first.call_count, 6)

    def test_archive_search_retains_no_redirect_rule(self):
        with patch.object(transport, '_urllib_once', return_value=(
                {'status':302, 'type':'', 'location':'https://archive.org/new'}, b'')) as first:
            with self.assertRaises(urllib.error.HTTPError):
                video_sources.archive_search('train', 5)
            self.assertEqual(first.call_count, 1)

    def test_post_uses_node_once_and_never_follows_redirect(self):
        with patch.object(transport, '_urllib_once') as first, \
             patch.object(transport, '_node_once', return_value=(
                 {'status':307, 'type':'', 'location':'https://elsewhere.invalid/'}, b'')) as node:
            with self.assertRaises(urllib.error.HTTPError):
                transport.read('https://model.invalid/api', 100, headers={'Authorization':'Bearer secret'},
                               body=b'{}', follow_redirects=True)
            first.assert_not_called()
            self.assertEqual(node.call_count, 1)

    def test_post_network_failure_not_retried(self):
        with patch.object(transport, '_urllib_once') as first, \
             patch.object(transport, '_node_once', side_effect=EOF) as node:
            with self.assertRaises(urllib.error.URLError):
                transport.read('https://model.invalid/api', 100, body=b'{}')
            first.assert_not_called()
            self.assertEqual(node.call_count, 1)

    def test_node_secrets_use_stdin_and_tls_cannot_be_disabled(self):
        done = subprocess.CompletedProcess([], 0, json.dumps(OK).encode()+b'\nbytes')
        with patch.dict(transport.os.environ, {'NODE_TLS_REJECT_UNAUTHORIZED':'0','NODE_OPTIONS':'--require unsafe'}), \
             patch.object(transport.subprocess, 'run', return_value=done) as run:
            data = transport._node_once('https://model.invalid', {'Authorization':'Bearer private-key'},
                                       b'private-body', 100, None, time.monotonic()+5, '/node')
        self.assertEqual(data[1], b'bytes')
        args,kwargs = run.call_args
        self.assertNotIn('private-key', str(args))
        self.assertNotIn('private-body', str(args))
        self.assertEqual(json.loads(kwargs['input'])['headers']['Authorization'], 'Bearer private-key')
        self.assertEqual(kwargs['env']['NODE_TLS_REJECT_UNAUTHORIZED'], '1')
        self.assertNotIn('NODE_OPTIONS', kwargs['env'])
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)

    def test_node_process_timeout_is_bounded(self):
        with patch.object(transport.subprocess, 'run', side_effect=subprocess.TimeoutExpired('node', 1)):
            with self.assertRaises(TimeoutError):
                transport._node_once('https://archive.org', {}, None, 10, None, time.monotonic()+1, '/node')

    def test_model_errors_do_not_expose_key(self):
        config = dict(base_url='https://model.invalid', api_key='private-key', model='qwen-test')
        with patch.object(transport, 'read', side_effect=urllib.error.URLError('private-key')):
            with self.assertRaises(search_agent.Unavailable) as error:
                search_agent.decide(config, {}, 6)
        self.assertNotIn('private-key', str(error.exception))


class NodeStreamTests(unittest.TestCase):
    """Exercise the actual fallback JavaScript without connecting to a server."""
    @classmethod
    def setUpClass(cls):
        cls.binary = transport.node_binary()
        if not cls.binary:
            raise unittest.SkipTest('Node is not installed')

    def run_fake_fetch(self, javascript, *, limit=10, mime=None, timeout=5):
        with patch.object(transport, '_NODE_READER', javascript + '\n' + transport._NODE_READER):
            return transport._node_once('https://archive.org/a', {}, None, limit, mime,
                                        time.monotonic()+timeout, self.binary)

    def test_binary_round_trip(self):
        meta,body = self.run_fake_fetch("global.fetch = async () => new Response(Uint8Array.from([0,255,2]), {headers:{'Content-Type':'video/mp4'}});", mime='video/mp4')
        self.assertEqual(meta['status'], 200)
        self.assertEqual(body, b'\x00\xff\x02')

    def test_stream_larger_than_limit_rejected(self):
        with self.assertRaisesRegex(ValueError, '大小上限'):
            self.run_fake_fetch("global.fetch = async () => new Response('12345678901');")

    def test_mime_mismatch_rejected(self):
        with self.assertRaisesRegex(ValueError, '文件类型'):
            self.run_fake_fetch("global.fetch = async () => new Response('123', {headers:{'Content-Type':'image/jpeg'}});", mime='video/mp4')

    def test_no_automatic_redirect_or_error_body_read(self):
        meta,body = self.run_fake_fetch("global.fetch = async (url, options) => { if(options.redirect !== 'manual') throw Error('unsafe'); return new Response('private upstream body', {status:302,headers:{Location:'https://outside.invalid'}}); };")
        self.assertEqual(meta['status'], 302)
        self.assertEqual(meta['location'], 'https://outside.invalid')
        self.assertEqual(body, b'')

    def test_abort_timeout(self):
        with self.assertRaises(TimeoutError):
            self.run_fake_fetch("global.fetch = (url, options) => new Promise((resolve,reject) => options.signal.addEventListener('abort', () => reject(Error('aborted'))));", timeout=0.25)


if __name__ == '__main__':
    unittest.main()
