"""A new clone must start without private output files or credentials."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import studio


class StudioBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'index.html').write_text('const R = {"title":"bundled"};\nconst STUDIO = false;', encoding='utf-8')
        self.patch_root = patch.object(studio, 'ROOT', str(self.root))
        self.patch_out = patch.object(studio, 'OUT', str(self.root / 'output'))
        self.patch_root.start()
        self.patch_out.start()
        self.addCleanup(self.patch_root.stop)
        self.addCleanup(self.patch_out.stop)

    def test_new_clone_loads_bundled_preview(self):
        result = studio.read_result()
        self.assertEqual(result['title'], 'bundled')
        self.assertTrue(result['preview_only'])
        self.assertFalse((self.root / 'output').exists())

    def test_real_run_takes_precedence(self):
        (self.root / 'output').mkdir()
        (self.root / 'output/result.json').write_text(json.dumps({'title': 'local'}), encoding='utf-8')
        self.assertEqual(studio.read_result(), {'title': 'local'})

    def test_missing_archive_reports_clear_error(self):
        (self.root / 'index.html').write_text('no archive', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '演示数据缺失'):
            studio.read_result()
