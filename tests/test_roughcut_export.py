import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import roughcut_export as export


class RoughcutExportTests(unittest.TestCase):
    def setUp(self):
        self.result = {'run_id': 'run1', 'total_s': 2.4, 'clips': {'m1': {}, 'm2': {}}, 'segments': [
            {'start': 0, 'dur': 2.4, 'subtitle': 'Hello\nworld', 'shots': [{'clip': 'm1'}],
             'stickers': [{'start': .3, 'dur': 1, 'text': 'Save now'}]}]}

    def test_swaps_do_not_change_source_result(self):
        before = copy.deepcopy(self.result)
        result = export.prepare(self.result, {'0': {'0': 'm2'}})
        self.assertEqual(result['segments'][0]['shots'][0]['clip'], 'm2')
        self.assertEqual(self.result, before)

    def test_invalid_swaps_rejected(self):
        for swaps in ([], {'-1': {'0': 'm2'}}, {'9': {'0': 'm2'}}, {'0': {'0': 'unknown'}}, {'0': {'-1': 'm2'}}):
            with self.subTest(swaps=swaps), self.assertRaises(ValueError):
                export.prepare(self.result, swaps)

    def test_limits(self):
        for duration in (0, 181, float('nan')):
            self.result['total_s'] = duration
            with self.assertRaises(ValueError):
                export.prepare(self.result, {})

    def test_subtitles_and_stickers_have_original_timing(self):
        text = export.subtitles(self.result)
        self.assertIn('0:00:00.00,0:00:02.40,Subtitle', text)
        self.assertIn('Hello\\Nworld', text)
        self.assertIn('0:00:00.30,0:00:01.30,Sticker', text)
        self.assertNotIn('{\\pos', export.ass_text(r'{\pos(1,2)}'))
        self.assertEqual(export.ass_time(59.999), '0:01:00.00')

    def test_local_media_must_exist_inside_repo(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / 'repo'; root.mkdir()
            secret = Path(folder) / 'outside'; secret.write_text('x')
            (root/'alias').symlink_to(secret)
            for path in ('../outside', 'missing.mp4', 'alias'):
                with self.assertRaises(ValueError):
                    export.local_file(root, path)
