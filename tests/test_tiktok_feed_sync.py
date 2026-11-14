import importlib.util
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('sync_tiktok_feed', Path(__file__).parents[1] / 'tools' / 'sync_tiktok_feed.py')
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class TikTokFeedTests(unittest.TestCase):
    def test_escaped_unicode_is_readable(self):
        row = {'webVideoUrl': 'https://www.tiktok.com/@rail/video/123',
               'text': r'Train \ud83d\ude86', 'createTimeISO': '2026-11-12T00:00:00Z'}
        self.assertEqual(sync.normalize_rows([row], '2026-11-14T01:00:00Z')[0]['title'], 'Train 🚆')

    def test_only_public_video_fields_are_exported_and_duplicates_removed(self):
        item = {'webVideoUrl': 'https://www.tiktok.com/@rail/video/123?token=private',
                'text': 'Sleep on a train', 'createTimeISO': '2026-11-12T00:00:00Z',
                'playCount': 1200, 'authorMeta': {'name': 'rail', 'email': 'private'},
                'secret': 'private'}
        result = sync.normalize_rows([item, item, {'errorCode': 'NOT_FOUND'}], '2026-11-14T01:00:00Z')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['original'], 'https://www.tiktok.com/@rail/video/123')
        self.assertIsNone(result[0]['metrics']['likes'])
        self.assertNotIn('private', str(result))

    def test_bad_links_future_dates_and_invalid_counts_are_rejected(self):
        item = {'webVideoUrl': 'https://www.tiktok.com/@rail/video/123',
                'text': 'A train', 'createTimeISO': '2026-11-12T00:00:00Z', 'playCount': 1}
        rows = [dict(item, webVideoUrl='javascript:alert(1)'),
                dict(item, createTimeISO='2027-01-01T00:00:00Z'), dict(item, playCount=-1)]
        self.assertEqual(sync.normalize_rows(rows, '2026-11-14T01:00:00Z'), [])
