"""Configuration tests use synthetic secrets only, never real credentials."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

import llm
import reference_settings as settings


class ReferenceSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / '.env'
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_atomic_write_is_private_and_compatible_with_model_config(self):
        result = settings.save_settings(self.root, {
            'VIDEO_BASE_URL': 'https://vision.example/v1/', 'VIDEO_MODEL': 'vision-model-v1',
            'VIDEO_API_KEY': 'fixture-video-token', 'PEXELS_API_KEY': 'fixture-search-token',
        })
        self.assertTrue(all(type(value) is bool for value in result.values()))
        self.assertTrue(result['vision'])
        self.assertTrue(result['search'])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertNotIn('fixture', repr(result))
        with patch.object(llm, 'ROOT', str(self.root)):
            cfg = llm.model_config('video')
            self.assertEqual(cfg['base_url'], 'https://vision.example/v1')
            self.assertEqual(cfg['model'], 'vision-model-v1')
            self.assertEqual(cfg['api_key'], 'fixture-video-token')

    def test_comments_other_settings_and_blank_keys_preserved(self):
        original = '# Existing comment\nSCRIPT_MODEL=keep-me\nVIDEO_API_KEY=old-fixture\nPEXELS_API_KEY=old-search\n'
        self.path.write_text(original)
        settings.save_settings(self.root, {'VIDEO_MODEL': 'new-model', 'VIDEO_API_KEY': '  ', 'PEXELS_API_KEY': ''})
        text = self.path.read_text()
        self.assertTrue(text.startswith(original))
        self.assertIn('VIDEO_MODEL=new-model\n', text)

    def test_status_obeys_fallback_and_environment_precedence(self):
        self.path.write_text('DEEPSEEK_API_KEY=fixture-fallback\n')
        result = settings.status(self.root)
        self.assertTrue(result['vision'])
        self.assertFalse(result['VIDEO_API_KEY'])
        with patch.dict(os.environ, {'VIDEO_API_KEY': 'fixture-environment', 'PEXELS_API_KEY': 'fixture-environment'}):
            result = settings.status(self.root)
            self.assertTrue(result['VIDEO_API_KEY'])
            self.assertTrue(result['search'])
            self.assertTrue(result['environment_override'])

    def test_newline_and_invalid_fields_rejected_without_writes(self):
        cases = [
            {'VIDEO_API_KEY': 'fixture\nSCRIPT_MODEL=hijack'},
            {'VIDEO_MODEL': 'model\rBAD=1'}, {'VIDEO_API_KEY': 'contains space'},
            {'VIDEO_MODEL': 10}, {'UNSUPPORTED': 'value'}, {}, [],
            {'VIDEO_MODEL': 'x' * 201}, {'PEXELS_API_KEY': 'x' * 4097},
        ]
        for body in cases:
            with self.subTest(body_type=type(body).__name__), self.assertRaises(ValueError):
                settings.save_settings(self.root, body)
            self.assertFalse(self.path.exists())

    def test_https_or_explicit_loopback_urls_only(self):
        accepted = ['https://vision.example/v1', 'http://localhost:1234/v1', 'http://127.0.0.1:8000/v1', 'http://[::1]:8000/v1']
        for url in accepted:
            settings.save_settings(self.root, {'VIDEO_BASE_URL': url})
        rejected = ['http://vision.example/v1', 'http://localhost.evil.example/v1', 'https://user:secret@vision.example',
                    'https://vision.example?key=secret', 'https://vision.example#fragment', 'file:///tmp/key',
                    'https://', 'https://vision.example:99999', 'https://bad host/v1', 'https://vision.example\\bad']
        for url in rejected:
            before = self.path.read_bytes()
            with self.subTest(url=url), self.assertRaises(ValueError):
                settings.save_settings(self.root, {'VIDEO_BASE_URL': url})
            self.assertEqual(self.path.read_bytes(), before)

    def test_duplicates_replaced_consistently(self):
        self.path.write_text('# comment\nVIDEO_MODEL=first\nOTHER=retained\nVIDEO_MODEL=last\n')
        settings.save_settings(self.root, {'VIDEO_MODEL': 'replacement'})
        text = self.path.read_text()
        self.assertEqual(text.count('VIDEO_MODEL='), 1)
        self.assertIn('# comment\n', text)
        self.assertIn('OTHER=retained\n', text)

    def test_blank_only_submission_does_not_create_config(self):
        result = settings.save_settings(self.root, {'VIDEO_API_KEY': ''})
        self.assertFalse(result['vision'])
        self.assertFalse(self.path.exists())

    def test_symlink_and_atomic_failure_preserve_existing_file(self):
        outside = self.root / 'other'
        outside.write_text('KEEP=unchanged\n')
        self.path.symlink_to(outside)
        with self.assertRaises(ValueError): settings.save_settings(self.root, {'VIDEO_MODEL': 'new'})
        self.assertEqual(outside.read_text(), 'KEEP=unchanged\n')
        self.path.unlink()
        self.path.write_text('KEEP=unchanged\n')
        with patch.object(settings.os, 'replace', side_effect=OSError('synthetic error')):
            with self.assertRaises(ValueError): settings.save_settings(self.root, {'VIDEO_MODEL': 'new'})
        self.assertEqual(self.path.read_text(), 'KEEP=unchanged\n')
        self.assertFalse(list(self.root.glob('.reference-settings-*')))


if __name__ == '__main__': unittest.main()
