import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import topic_feed


class TopicFeedTests(unittest.TestCase):
    def item(self, ident, published, url):
        return {'id': ident, 'title': 'Train trip', 'publishedAt': published,
                'source': {'name': 'Rail'}, 'links': {'original': url}}

    def test_only_real_video_links_and_recent_publication_are_recent(self):
        new = self.item('new', '2026-11-11T08:00:00Z', 'https://www.youtube.com/watch?v=abc')
        old = self.item('old', '2026-01-01T00:00:00Z', 'https://www.tiktok.com/@rail/video/123')
        unknown = self.item('unknown', None, 'https://www.instagram.com/reel/abc/')
        article = self.item('article', '2026-11-11T09:00:00Z', 'https://ads.tiktok.com/business/case')
        fake = self.item('fake', '2026-11-11T09:00:00Z', 'https://youtube.com.example.com/watch?v=abc')
        def read(url):
            if '/api/v1/' in url:
                return {'items': [new, new, old, unknown, article, fake]}
            if '/pool?' in url:
                return {'items': [old, unknown, article]}
            return {'old': old, 'unknown': unknown, 'article': article}[url.rsplit('/', 1)[-1]]
        with patch.object(topic_feed, 'read_json', side_effect=read), patch.object(topic_feed.llm, 'load_env', return_value={}):
            result = topic_feed.read_feed(datetime(2026, 11, 11, 12, tzinfo=timezone.utc))
        self.assertEqual([x['id'] for x in result['recent']], ['new'])
        self.assertEqual([x['id'] for x in result['history']], ['old', 'unknown'])
        self.assertIsNone(result['history'][1]['published_at'])

    def test_service_failure_is_not_an_empty_success(self):
        with patch.object(topic_feed, 'read_json', side_effect=OSError('private diagnostic')), patch.object(topic_feed.llm, 'load_env', return_value={}):
            result = topic_feed.read_feed()
        self.assertIsNone(result['recent'])
        self.assertIsNone(result['history'])
        self.assertEqual(set(result['errors']), {'recent', 'history'})
        self.assertNotIn('private diagnostic', str(result))

    def test_configuration_status_never_exposes_secret(self):
        with patch.object(topic_feed.llm, 'load_env', return_value={'YOUTUBE_API_KEY': 'test-secret'}), patch.object(topic_feed, 'read_json', return_value={'items': []}):
            result = topic_feed.read_feed()
        self.assertIn('采集器待接', result['platforms'][0]['status'])
        self.assertNotIn('test-secret', str(result))

    def test_video_classifier_rejects_articles_and_script_urls(self):
        for url in ['javascript:alert(1)', 'https://www.tiktok.com/@rail', 'https://www.instagram.com/blog/abc', 'https://youtube.com.example.com/watch?v=x']:
            self.assertIsNone(topic_feed.video_platform(url))
        self.assertEqual(topic_feed.video_platform('https://www.instagram.com/reel/xyz/'), 'Instagram')
