"""Content screening with fixture models only; no approval or external network."""
import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import reference_candidate_review as review
import reference_video as video


def positive(offset=1):
    return {'match': True, 'src_in': offset, 'checks': {key: True for key in review.CHECKS},
        'reason': 'Fixture: all seven observed conditions agree.'}


def negative():
    return {'match': False, 'src_in': 0, 'checks': {key: key != 'action' for key in review.CHECKS},
        'reason': 'Fixture: subject is standing still instead of passing.'}


class FixtureService:
    def __init__(self, root):
        self.root = Path(root)
        self.responses = []
        self.calls = []
        self.saves = []
        self.cancelled = False

    def directory(self, jid):
        directory = self.root / 'jobs' / jid
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def check(self, job):
        if self.cancelled:
            raise video.Cancelled('Fixture cancellation')

    def save(self, job, **changes):
        job.update(changes)
        self.saves.append(copy.deepcopy(job))
        (self.directory(job['id']) / 'job.json').write_text(json.dumps(job, allow_nan=False))

    def model(self, job, system, content, tokens):
        self.calls.append({'system': system, 'content': content, 'tokens': tokens})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        job['usage'].append({'provider': 'fixture', 'call': len(self.calls)})
        self.save(job)
        return copy.deepcopy(response)


class ContentReviewTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.service = FixtureService(self.root)
        shot = {'id': 's01', 'start_frame': 0, 'end_frame': 60,
            'subject': 'train', 'action': 'passing', 'shot_size': 'medium', 'scene': 'railway',
            'direction': 'left to right', 'camera': 'fixed', 'composition': 'centered'}
        self.job = {'id': 'fixture', 'analysis': {'shots': [shot]},
            'technical': {'width': 1080, 'height': 1920, 'fps': 30, 'total_frames': 60},
            'gaps': [{'shot_id': 's01'}], 'usage': [], 'matches': {},
            'material_search': {'shots': {'s01': {'candidate_items': []}}}}
        self.files = []
        self.add_asset('first')
        self.clock = 100
        self.frame_calls = []
        self.prober = patch.object(video, 'probe', return_value={'duration': 10, 'fps': 30, 'width': 1920, 'height': 1080})
        self.probe = self.prober.start()
        self.addCleanup(self.prober.stop)
        self.framer = patch.object(video, 'frames', side_effect=self.fake_frames)
        self.frames = self.framer.start()
        self.addCleanup(self.framer.stop)
        bridge = patch.object(review.legacy, 'downloaded_videos', create=True,
            side_effect=lambda root, asset_ids: [copy.deepcopy(row) for row in self.files if row['asset_id'] in asset_ids])
        self.bridge = bridge.start()
        self.addCleanup(bridge.stop)
        timer = patch.object(review.time, 'time', side_effect=lambda: self.clock)
        timer.start()
        self.addCleanup(timer.stop)

    def add_asset(self, name, **changes):
        source = self.root / (name + '.mp4')
        source.write_bytes(('Fixture bytes, not real video: ' + name).encode())
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.files.append({'asset_id': digest, 'path': str(source.resolve()), 'file_sha256': digest, 'duration': 999})
        self.job['material_search']['shots']['s01']['candidate_items'].append({
            'id': len(self.files), 'asset_id': digest, 'kind': 'video',
            'downloaded': True, 'approved': False, **changes})
        return digest

    def fake_frames(self, path, directory, times, prefix, target):
        self.frame_calls.append({'path': path, 'times': times, 'prefix': prefix, 'target': copy.deepcopy(target)})
        return [{'type': 'text', 'text': 'Fixture decoded cropped frame %.4f' % t} for t in times]

    def record(self, index=0):
        return self.job['material_search']['content_reviews']['s01'][self.files[index]['asset_id']]

    def test_match_requires_eight_cropped_frames_then_four_interval_frames(self):
        self.service.responses = [positive(1.25), {'match': True, 'reason': 'Fixture interval remains a passing train.'}]
        original_candidates = copy.deepcopy(self.job['material_search']['shots'])
        result = review.review_downloaded(self.service, self.job)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual([len(call['times']) for call in self.frame_calls], [8, 4])
        for call in self.frame_calls:
            self.assertEqual(call['target'], self.job['technical'])
            self.assertEqual(call['path'], Path(self.files[0]['path']))
        self.assertEqual(self.frame_calls[1]['times'], [1.35, 1.95, 2.55, 3.15])
        record = self.record()
        self.assertEqual(record['status'], 'content_match')
        self.assertEqual((record['src_in'], record['src_out']), (1.25, 3.25))
        self.assertTrue(record['requires_library_approval'])
        self.assertEqual(record['proof']['overview']['usage_start'], 0)
        self.assertEqual(record['proof']['interval']['usage_end'], 2)
        self.assertEqual(self.job['matches'], {})
        self.assertEqual(self.job['material_search']['shots'], original_candidates)
        self.assertNotIn('approved', record)
        self.assertFalse(review.needs_review(self.job))
        review.review_downloaded(self.service, self.job)
        self.assertEqual(len(self.service.calls), 2)

    def test_known_overview_mismatch_does_not_spend_interval_call(self):
        self.service.responses = [negative()]
        result = review.review_downloaded(self.service, self.job)
        self.assertEqual(result['model_calls'], 1)
        self.assertEqual(self.record()['status'], 'rejected')
        self.assertEqual(self.record()['rejection'], 'sampled_frames_mismatch')
        self.assertEqual(len(self.frame_calls), 1)
        self.assertFalse(review.needs_review(self.job))

    def test_interval_mismatch_is_content_rejection_without_library_mutation(self):
        self.service.responses = [positive(), {'match': False, 'reason': 'Fixture train disappears during requested interval.'}]
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['status'], 'rejected')
        self.assertEqual(self.record()['rejection'], 'interval_content_mismatch')
        self.assertFalse(self.job['material_search']['shots']['s01']['candidate_items'][0]['approved'])

    def test_actual_short_duration_rejects_without_model_despite_bridge_metadata(self):
        self.probe.return_value['duration'] = 1.9
        result = review.review_downloaded(self.service, self.job)
        self.assertEqual(result['model_calls'], 0)
        self.assertEqual(self.record()['status'], 'rejected')
        self.assertEqual(self.record()['rejection'], 'insufficient_duration')
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.frame_calls, [])

    def test_long_original_uses_bridge_measured_metadata_without_importer_limit(self):
        self.files[0]['metadata'] = {'duration': 287.23, 'width': 1920, 'height': 1080, 'fps': 30}
        self.probe.side_effect = video.media.MediaError('Reference importer intentionally limits inputs to 180 seconds')
        self.service.responses = [positive(240), {'match': True, 'reason': 'Fixture: valid interval near end of long source.'}]
        review.review_downloaded(self.service, self.job)
        self.probe.assert_not_called()
        self.assertEqual(self.record()['status'], 'content_match')
        self.assertEqual(self.record()['source_duration'], 287.23)
        self.assertEqual(self.record()['src_out'], 242)
        self.assertTrue(all(240 <= value <= 242 for value in self.frame_calls[1]['times']))

    def test_one_observed_mismatch_rejects_with_other_fields_unknown(self):
        checks = {key: None for key in review.CHECKS}
        checks['subject'] = False
        self.service.responses = [{'match':False,'reason':'Animated coffee has no woman.', 'checks':checks}]
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['status'], 'rejected')
        self.assertIsNone(self.record()['checks']['action'])
        self.assertEqual(len(self.service.calls), 1)

    def test_all_unknown_checks_never_become_a_rejection(self):
        self.service.responses = [{'match':False,'reason':'No clear evidence.', 'checks':{key:None for key in review.CHECKS}}]
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['status'], 'error')

    def test_unknown_response_retries_twice_with_backoff_and_never_rejects(self):
        self.service.responses = [{'match': None, 'reason': 'Unable to establish direction.'},
                                  {'match': None, 'reason': 'Direction remains unknown.'}]
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['status'], 'error')
        self.assertEqual(self.record()['next_retry_at'], 115)
        self.assertFalse(review.needs_review(self.job, now=114))
        self.clock = 114
        review.review_downloaded(self.service, self.job)
        self.assertEqual(len(self.service.calls), 1)
        self.clock = 115
        self.assertTrue(review.needs_review(self.job))
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['error_count'], 2)
        self.assertTrue(self.record()['retry_exhausted'])
        self.assertEqual(self.record()['status'], 'error')
        self.clock = 500
        self.assertFalse(review.needs_review(self.job))
        review.review_downloaded(self.service, self.job)
        self.assertEqual(len(self.service.calls), 2)

    def test_malformed_checks_and_invalid_offsets_are_errors_not_rejections(self):
        responses = [positive(-1), positive(9), positive(True), positive(float('nan')),
            {'match': True, 'src_in': 0, 'checks': {'subject': True}, 'reason': 'Incomplete checks.'},
            {**negative(), 'uncertain': True}]
        for response in responses:
            with self.subTest(response=response):
                self.job['material_search']['content_reviews'] = {}
                self.service.responses = [response]
                result = review.review_downloaded(self.service, self.job)
                self.assertEqual(result['model_calls'], 1)
                self.assertEqual(self.record()['status'], 'error')

    def test_pending_interval_resumes_from_durable_record_with_one_call_budget(self):
        self.service.responses = [positive(), {'match': True, 'reason': 'Fixture interval verified.'}]
        first = review.review_downloaded(self.service, self.job, max_new=1)
        self.assertEqual(first['model_calls'], 1)
        self.assertEqual(self.record()['phase'], 'verify_pending')
        self.assertEqual(self.record()['status'], 'checking')
        self.assertTrue(review.needs_review(self.job))
        self.job = json.loads((self.service.directory(self.job['id']) / 'job.json').read_text())
        second = review.review_downloaded(self.service, self.job, max_new=1)
        self.assertEqual(second['model_calls'], 1)
        self.assertEqual([len(call['times']) for call in self.frame_calls], [8, 4])
        self.assertEqual(self.record()['status'], 'content_match')

    def test_interval_network_retry_preserves_overview_and_usage(self):
        self.service.responses = [positive(), video.media.MediaError('Fixture connection timeout'),
                                  {'match': True, 'reason': 'Fixture interval now visible.'}]
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['status'], 'error')
        self.assertEqual(self.record()['phase'], 'verify_pending')
        self.clock = 115
        review.review_downloaded(self.service, self.job)
        self.assertEqual([len(call['times']) for call in self.frame_calls], [8, 4, 4])
        self.assertEqual(len(self.service.calls), 3)
        self.assertEqual(self.record()['status'], 'content_match')

    def test_long_source_negative_is_deferred_without_repeat_or_whole_source_claim(self):
        self.files[0]['metadata'] = {'duration':287.0}
        self.service.responses = [negative()]
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['status'], 'deferred')
        self.assertEqual(self.record()['unsampled_content'], 'unverified')
        self.assertFalse(review.needs_review(self.job))
        self.add_asset('another_short_source')
        self.service.responses = [negative()]
        review.review_downloaded(self.service, self.job)
        self.assertEqual(len(self.service.calls), 2)
        self.assertEqual(self.record(0)['status'], 'deferred')

    def test_model_call_budget_bounds_multiple_candidates(self):
        self.add_asset('second')
        self.add_asset('third')
        self.service.responses = [negative(), negative(), negative()]
        result = review.review_downloaded(self.service, self.job)
        self.assertEqual(result['model_calls'], 2)
        self.assertEqual(len(self.service.calls), 2)
        self.assertTrue(review.needs_review(self.job))
        result = review.review_downloaded(self.service, self.job)
        self.assertEqual(result['model_calls'], 1)
        self.assertFalse(review.needs_review(self.job))

    def test_total_five_asset_budget_survives_reload_reordering_and_new_candidates(self):
        for number in range(2, 8):
            self.add_asset('asset_%s' % number)
        admitted = [row['asset_id'] for row in self.files[:5]]
        deferred = [row['asset_id'] for row in self.files[5:]]
        self.service.responses = [negative() for _ in range(5)]
        first = review.review_downloaded(self.service, self.job)
        self.assertEqual(first['budget_deferred'], 2)
        self.assertEqual(self.job['material_search']['content_review_budget']['s01'], admitted)
        self.job = json.loads((self.service.directory(self.job['id']) / 'job.json').read_text())
        self.job['material_search']['shots']['s01']['candidate_items'].reverse()
        for _ in range(4):
            review.review_downloaded(self.service, self.job)
        self.assertEqual(len(self.service.calls), 5)
        records = self.job['material_search']['content_reviews']['s01']
        self.assertTrue(all(records[aid]['status'] == 'rejected' for aid in admitted))
        self.assertTrue(all(records[aid]['status'] == 'deferred' for aid in deferred))
        self.assertFalse(review.needs_review(self.job))
        self.add_asset('late_candidate')
        self.assertFalse(review.needs_review(self.job))
        review.review_downloaded(self.service, self.job)
        self.assertEqual(len(self.service.calls), 5)
        self.assertEqual(self.record(-1)['status'], 'deferred')
        self.assertEqual(self.job['material_search']['content_review_budget']['s01'], admitted)

    def test_shot_target_or_verified_file_hash_change_invalidates_cache(self):
        self.service.responses = [negative(), negative(), negative(), negative()]
        review.review_downloaded(self.service, self.job)
        old = self.record()['request_fingerprint']
        self.job['analysis']['shots'][0]['direction'] = 'right to left'
        self.assertTrue(review.needs_review(self.job))
        review.review_downloaded(self.service, self.job)
        self.assertNotEqual(self.record()['request_fingerprint'], old)
        self.job['technical']['width'] = 1920
        self.assertTrue(review.needs_review(self.job))
        review.review_downloaded(self.service, self.job)
        digest = hashlib.sha256(b'Changed bridge-verified source').hexdigest()
        self.files[0]['file_sha256'] = digest
        self.job['material_search']['shots']['s01']['candidate_items'][0]['file_sha256'] = digest
        self.assertTrue(review.needs_review(self.job))
        review.review_downloaded(self.service, self.job)
        self.assertEqual(len(self.service.calls), 4)
        self.assertEqual(self.record()['file_sha256'], digest)

    def test_only_safe_bridge_paths_used_and_missing_file_is_unknown(self):
        self.job['material_search']['shots']['s01']['candidate_items'][0]['path'] = '/untrusted/from/candidate.mp4'
        self.files = []
        result = review.review_downloaded(self.service, self.job)
        record = next(iter(self.job['material_search']['content_reviews']['s01'].values()))
        self.assertEqual(record['status'], 'error')
        self.assertEqual(result['model_calls'], 0)
        self.probe.assert_not_called()

    def test_bridge_error_is_bounded_unknown_without_model_or_approval(self):
        self.bridge.side_effect = review.legacy.LegacyError('Fixture: local library unavailable', retryable=True)
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['status'], 'error')
        self.assertEqual(self.record()['next_retry_at'], 115)
        self.clock = 115
        review.review_downloaded(self.service, self.job)
        self.assertEqual(self.record()['error_count'], 2)
        self.assertFalse(review.needs_review(self.job, now=500))
        self.assertEqual(self.service.calls, [])

    def test_zero_budget_and_invalid_budgets_do_not_contact_library(self):
        result = review.review_downloaded(self.service, self.job, max_new=0)
        self.assertEqual(result['model_calls'], 0)
        self.assertTrue(result['needs_review'])
        self.bridge.assert_not_called()
        for value in (True, -1, 3, 1.5):
            with self.assertRaises(ValueError):
                review.review_downloaded(self.service, self.job, max_new=value)

    def test_ready_predicate_uses_no_bridge_and_skips_approved_images_or_missing_downloads(self):
        self.assertTrue(review.needs_review(self.job))
        self.bridge.assert_not_called()
        candidate = self.job['material_search']['shots']['s01']['candidate_items'][0]
        for change in ({'approved': True}, {'kind': 'image'}, {'downloaded': False}):
            original = dict(candidate)
            candidate.update(change)
            self.assertFalse(review.needs_review(self.job))
            candidate.clear(); candidate.update(original)

    def test_unanalyzed_job_has_no_pending_review_without_technical_metadata(self):
        self.job['analysis'] = None
        self.job['technical'] = None
        self.assertFalse(review.needs_review(self.job))
        self.bridge.assert_not_called()
        self.probe.assert_not_called()

    def test_cancellation_is_propagated_without_recording_false_content_error(self):
        self.service.cancelled = True
        with self.assertRaises(video.Cancelled):
            review.review_downloaded(self.service, self.job)
        self.assertEqual(self.service.calls, [])
        self.assertEqual(self.job['material_search'].get('content_reviews', {}), {})

    def test_actual_ffmpeg_crop_frames_with_fixture_model_only(self):
        try:
            exe = video.tool('ffmpeg'); video.tool('ffprobe')
        except video.media.MediaError as exc:
            self.skipTest(str(exc))
        self.prober.stop(); self.framer.stop()
        source = self.root / 'actual.mp4'
        result = subprocess.run([exe, '-v', 'error', '-y', '-f', 'lavfi', '-i',
            'color=c=red:s=160x90:r=10:d=5', '-vf', 'drawbox=x=50:y=0:w=60:h=90:color=0x00FF00:t=fill',
            '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', str(source)], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        self.files[0].update(path=str(source.resolve()), file_sha256=digest)
        self.job['technical'].update(width=64, height=96)
        self.service.responses = [positive(1), {'match': True, 'reason': 'Synthetic center stripe; fixture model, not real semantic matching.'}]
        result = review.review_downloaded(self.service, self.job)
        self.assertEqual(result['model_calls'], 2)
        from PIL import Image
        files = [name for phase in self.record()['proof'].values() for name in phase['frame_files']]
        self.assertEqual(len(files), 12)
        for name in files:
            with Image.open(self.service.directory(self.job['id']) / name) as frame:
                self.assertEqual(frame.size, (640, 960))
                r, g, b = frame.getpixel((320, 480))
                self.assertGreater(g, r + 150)
                self.assertGreater(g, b + 150)
        self.assertEqual(self.job['matches'], {})


if __name__ == '__main__':
    unittest.main()
