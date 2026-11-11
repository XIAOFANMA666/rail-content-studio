import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import studio
import video_trends as v

NOW = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)


def fixture(platform='TikTok', views=100):
    return {'title': 'Train journey', 'url': 'https://www.tiktok.com/@rail/video/123?track=1' if platform == 'TikTok' else 'https://www.instagram.com/reel/ABC/',
            'author': 'Rail', 'published_at': '2026-11-10T10:00:00Z', 'metrics': {'views': views, 'likes': 0}}


class VideoTrendsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.out = self.tmp.name
        self.cfg = {key: '' for key in v.PLATFORMS.values()}
        self.cfg.update(YOUTUBE_API_KEY='yt-secret', VIDEO_TRENDS_TIKTOK_FEED_URL='https://feed.example/tiktok?key=private',
                        VIDEO_TRENDS_INSTAGRAM_FEED_URL='https://feed.example/instagram', VIDEO_TRENDS_TIKTOK_TOKEN='bearer-secret')
        p = patch.object(v, 'config', return_value=self.cfg); p.start(); self.addCleanup(p.stop)

    def collect(self, platforms=None, now=NOW):
        return v.collect(self.out, {'query': 'train travel', 'platforms': platforms or ['TikTok']}, now)

    def test_youtube_search_statistics_and_hidden_metrics(self):
        replies = [{'items': [{'id': {'videoId': 'abcdefghijk'}}, {'id': {'videoId': 'abcdefghijk'}}]},
                   {'items': [{'id': 'abcdefghijk', 'snippet': {'title': 'Rail', 'channelTitle': 'Test', 'publishedAt': '2026-11-10T00:00:00Z'},
                               'statistics': {'viewCount': '400', 'commentCount': '0'}}]}]
        with patch.object(v, 'request_json', side_effect=replies) as request:
            result = self.collect(['YouTube'])
        self.assertEqual(len(result['items']), 1)
        row = result['items'][0]
        self.assertEqual(row['metrics'], {'views': 400, 'likes': None, 'comments': 0, 'shares': None})
        search = parse_qs(urlsplit(request.call_args_list[0].args[0]).query)
        self.assertEqual(search['q'], ['train travel']); self.assertEqual(search['regionCode'], ['GB'])
        self.assertEqual(request.call_args_list[0].args[1], {'X-Goog-Api-Key': 'yt-secret'})
        self.assertNotIn('yt-secret', json.dumps(result))

    def test_feed_contract_auth_query_and_dedup_updates(self):
        with patch.object(v, 'request_json', return_value={'items': [fixture(), fixture()]}) as request:
            first = self.collect()
        self.assertEqual(len(first['items']), 1)
        self.assertEqual(request.call_args.args[1], {'Authorization': 'Bearer bearer-secret'})
        self.assertEqual(parse_qs(urlsplit(request.call_args.args[0]).query)['limit'], ['25'])
        with patch.object(v, 'request_json', return_value={'items': [fixture(views=200)]}):
            second = self.collect(now=NOW+timedelta(minutes=1))
        self.assertEqual(second['items'][0]['metrics']['views'], 200)
        self.assertNotIn('private', json.dumps(second)); self.assertNotIn('track', json.dumps(second))
        self.assertEqual(json.loads((Path(self.out)/'video-trends.json').read_text())['items'], second['items'])

    def test_partial_failure_retains_previous_data(self):
        with patch.object(v, 'request_json', return_value={'items': [fixture()]}):
            self.collect()
        def request(url, headers):
            if '/tiktok' in url: raise v.FeedError('暂时不可用')
            return {'items': [fixture('Instagram')]}
        with patch.object(v, 'request_json', side_effect=request):
            result = self.collect(['TikTok', 'Instagram'], NOW+timedelta(minutes=1))
        self.assertEqual(len(result['items']), 2)
        status = {p['name']: p for p in result['platforms']}
        self.assertIn('失败', status['TikTok']['status']); self.assertIsNotNone(status['TikTok']['succeeded_at'])
        self.assertIn('成功', status['Instagram']['status'])

    def test_missing_keys_and_read_never_call_network(self):
        self.cfg.update({key: '' for key in v.PLATFORMS.values()})
        with patch.object(v, 'request_json') as request:
            result = v.read(self.out)
            self.assertTrue(all(not p['configured'] for p in result['platforms']))
            self.collect(list(v.PLATFORMS)); request.assert_not_called()

    def test_zero_results_is_success_not_error(self):
        with patch.object(v, 'request_json', return_value={'items': []}):
            result = self.collect()
        status = result['platforms'][1]
        self.assertEqual(status['count'], 0); self.assertIsNone(status['error'])

    def test_invalid_payload_does_not_replace_previous_data(self):
        with patch.object(v, 'request_json', return_value={'items': [fixture()]}): self.collect()
        with patch.object(v, 'request_json', return_value={'data': []}):
            result = self.collect(now=NOW+timedelta(minutes=1))
        self.assertEqual(len(result['items']), 1); self.assertIn('items', result['platforms'][1]['error'])

    def test_old_provider_snapshot_does_not_regress_stats(self):
        fresh = fixture(views=200); fresh['metrics_at'] = NOW.isoformat()
        old = fixture(views=100); old['metrics_at'] = (NOW-timedelta(days=1)).isoformat()
        with patch.object(v, 'request_json', return_value={'items': [fresh]}): self.collect()
        with patch.object(v, 'request_json', return_value={'items': [old]}):
            result = self.collect(now=NOW+timedelta(minutes=1))
        self.assertEqual(result['items'][0]['metrics']['views'], 200)

    def test_invalid_urls_metrics_and_dates(self):
        for url in ['javascript:alert(1)', 'https://tiktok.com.evil/@rail/video/123', 'https://user:pass@tiktok.com/@rail/video/123', 'https://tiktok.com/@rail', 'https://instagram.com/p/123']:
            row = fixture(); row['url'] = url
            with self.assertRaises(v.FeedError): v.normalize('TikTok', row, NOW, 'test')
        for value in [-1, True, 'NaN', 1.5, '1.2K']:
            with self.assertRaises(v.FeedError): v.count(value)
        row = fixture(); row['published_at'] = '2026-11-10'
        with self.assertRaises(v.FeedError): v.normalize('TikTok', row, NOW, 'test')

    def test_rate_limit_and_concurrent_lock(self):
        with patch.object(v, 'request_json', return_value={'items': []}) as request:
            self.collect()
            with self.assertRaises(v.BusyError): self.collect()
            self.assertEqual(request.call_count, 1)
        with v.LOCK:
            with self.assertRaises(v.BusyError): self.collect(now=NOW+timedelta(minutes=1))

    def test_corrupt_storage_not_overwritten_and_expiry(self):
        path = Path(self.out)/'video-trends.json'; path.write_text('{broken')
        with self.assertRaises(v.FeedError): self.collect()
        self.assertEqual(path.read_text(), '{broken'); path.unlink()
        with patch.object(v, 'request_json', return_value={'items': [fixture()]}): self.collect()
        self.assertEqual(v.load(self.out, NOW+timedelta(days=31))['items'], [])

    def test_http_errors_redacted_and_redirect_blocked(self):
        opener = unittest.mock.MagicMock()
        opener.open.side_effect = HTTPError('https://feed.example/?secret', 403, 'secret-detail', {}, None)
        with patch.object(v, 'build_opener', return_value=opener):
            with self.assertRaises(v.FeedError) as ctx: v.request_json('https://feed.example/')
        self.assertNotIn('secret', str(ctx.exception)); self.assertIn('权限', str(ctx.exception))
        self.assertIsNone(v.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_bad_request_rejected_before_network(self):
        for body in [None, {'query': ''}, {'query':'train','days':True}, {'query':'train','platforms':['Unknown']}, {'query':'train','region':'GB&key=x'}]:
            with self.assertRaises(v.FeedError): v.collect(self.out, body, NOW)


class VideoTrendsAPITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.server = studio.ThreadingHTTPServer(('127.0.0.1', 0), studio.H)
        self.port = self.server.server_port
        for name, value in [('OUT', self.tmp.name), ('PORT', self.port)]:
            p = patch.object(studio, name, value); p.start(); self.addCleanup(p.stop)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def request(self, method, path, body=None, origin=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {'Origin': origin or 'http://localhost:%s'%self.port, 'Content-Type':'application/json'})
        r=c.getresponse(); result=(r.status,json.loads(r.read())); c.close(); return result

    def test_origin_and_body_validation(self):
        with patch.object(v, 'collect') as collect:
            self.assertEqual(self.request('POST','/api/video-trends/collect', {'query':'train'}, 'https://evil.test')[0],403)
            self.assertEqual(self.request('POST','/api/video-trends/collect', {'query':'x'*5000})[0],400)
            collect.assert_not_called()

    def test_get_is_read_only_and_post_persists(self):
        cfg = {key: '' for key in v.PLATFORMS.values()}; cfg['VIDEO_TRENDS_TIKTOK_FEED_URL']='https://feed.example/'
        with patch.object(v, 'config', return_value=cfg), patch.object(v, 'request_json', return_value={'items':[fixture()]}) as net:
            self.assertEqual(self.request('GET','/api/video-trends')[0],200); net.assert_not_called()
            status, data=self.request('POST','/api/video-trends/collect',{'query':'train','platforms':['TikTok']})
            self.assertEqual(status,200); self.assertEqual(len(data['items']),1)
            self.assertTrue((Path(self.tmp.name)/'video-trends.json').exists())
            self.assertEqual(self.request('GET','/api/video-trends')[1]['items'],data['items'])
            self.assertEqual(net.call_count,1)
