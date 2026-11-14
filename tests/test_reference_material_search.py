"""Offline scheduler contracts using the real embedded Skill and a fake library.

No provider calls, HTTP, downloads or media approval occur in this suite. The
fake service writes actual job checkpoints so submit ordering survives reloads.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reference_material_search as search


PROJECT = Path(__file__).resolve().parents[1]


def fixture_job(count=1):
    shots = []
    for index in range(count):
        shots.append({'id': 'shot_%d' % (index + 1), 'start_frame': index * 90,
            'end_frame': (index + 1) * 90, 'subject': 'train', 'action': 'passing',
            'shot_size': 'medium', 'scene': 'railway platform', 'direction': 'left to right',
            'camera': 'fixed camera', 'composition': 'train fully visible', 'place': '',
            'motion_required': True, 'queries': ['train passing medium platform',
                'railway passenger train side', 'commuter train fixed camera']})
    return {'id': 'scheduler_fixture', 'technical': {'width': 1080, 'height': 1920,
        'total_frames': count * 90, 'fps_num': 30, 'fps_den': 1, 'fps': 30},
        'analysis': {'shots': shots}, 'gaps': [{'shot_id': shot['id'], 'requirement': shot,
            'reason': 'No matching reviewed interval'} for shot in shots], 'rejected': {}}


class FakeService:
    def __init__(self, out):
        self.root = PROJECT
        self.out = Path(out)
        self.saves = []
        self.checks = 0

    def directory(self, jid):
        path = self.out / jid
        path.mkdir(parents=True, exist_ok=True)
        return path

    def save(self, job, **updates):
        job.update(updates)
        self.saves.append(copy.deepcopy(job))
        (self.directory(job['id']) / 'job.json').write_text(json.dumps(job), encoding='utf-8')

    def check(self, job):
        self.checks += 1

    def reload(self, jid='scheduler_fixture'):
        return json.loads((self.directory(jid) / 'job.json').read_text(encoding='utf-8'))


class FakeLegacy:
    LegacyError = search.legacy.LegacyError
    LegacyBusy = search.legacy.LegacyBusy

    def __init__(self, service):
        self.service = service
        self.tasks = {}
        self.requests = []
        self.lookups = []
        self.reads = []
        self.resumes = []
        self.by_fingerprint = {}
        self.exported = []
        self.export_calls = []
        self.task_errors = set()
        self.find_error = False
        self.service_error = False
        self.lose_next_response = False
        self.agent_configured = True
        self.next_id = 101

    def ensure_running(self, root, out):
        if self.service_error:
            raise self.LegacyError('Fixture: legacy service connection failed', retryable=True)
        return {'available': True, 'running': True, 'started': False}

    def config(self, root):
        return {'available': True, 'agent_configured': self.agent_configured}

    def find_request(self, root, fingerprint):
        self.lookups.append(fingerprint)
        if self.find_error:
            raise self.LegacyError('Fixture: cannot determine existing request', retryable=True)
        tid = self.by_fingerprint.get(fingerprint)
        return {'id': tid} if tid is not None else None

    def search(self, request):
        # At the exact mutation boundary, read the durable checkpoint, not the
        # same in-memory object the implementation is about to mutate.
        context = request['reference_context']
        saved = self.service.reload(context['job_id'])
        pending = saved['material_search']['shots'][context['shot_id']]['pending_submission']
        if pending['request_fingerprint'] != request['request_fingerprint'] or pending['request'] != request:
            raise AssertionError('Search request was not persisted before submission')
        self.requests.append(copy.deepcopy(request))
        tid = self.next_id
        self.next_id += 1
        self.by_fingerprint[request['request_fingerprint']] = tid
        self.tasks[tid] = {'id': tid, 'state': 'queued', 'result': {},
            'candidates': [], 'candidate_items': []}
        if self.lose_next_response:
            self.lose_next_response = False
            raise self.LegacyError('Fixture: server committed task; HTTP response was lost', retryable=True)
        return {'task_id': tid, 'provider': 'legacy-commons', 'kind': request['kind'], 'mode': request['mode']}

    def task(self, tid, **kwargs):
        self.reads.append(tid)
        if tid in self.task_errors:
            raise self.LegacyError('Fixture: task state connection failed', retryable=True)
        return copy.deepcopy(self.tasks[tid])

    def resume_search(self, tid):
        self.resumes.append(tid)
        self.tasks[tid]['state'] = 'queued'
        retries = self.tasks[tid].get('result', {}).get('network_retries', 0)
        self.tasks[tid]['result'] = {'network_retries': retries + 1}
        return {'id': tid, 'resumed': True}

    def export_approved_videos(self, root, *, asset_ids=None):
        self.export_calls.append(set(asset_ids or []))
        return [copy.deepcopy(clip) for clip in self.exported
                if asset_ids is None or clip['legacy_asset_id'] in asset_ids]


class MaterialSearchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.service = FakeService(temp.name)
        self.legacy = FakeLegacy(self.service)
        patcher = patch.object(search, 'legacy', self.legacy)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tick(self, job, now):
        return search.tick(self.service, job, now=now)

    def row(self, job, sid='shot_1'):
        return job['material_search']['shots'][sid]

    def finish(self, tid, **changes):
        self.legacy.tasks[tid].update(state='done', result={}, **changes)

    def test_load_plan_reads_real_skill_and_keeps_full_motion_requirements(self):
        job = fixture_job()
        policy, plan = search.load_plan(PROJECT, job['analysis'], job['technical'])
        expected = hashlib.sha256((PROJECT / search.SKILL_DIR / 'SKILL.md').read_bytes()).hexdigest()
        self.assertEqual(plan['skill_sha256'], expected)
        self.assertEqual(policy['max_concurrent_tasks'], 2)
        self.assertEqual(policy['max_external_tasks_per_shot'], 2)
        shot = plan['shots'][0]
        self.assertFalse(shot['blockers'])
        self.assertEqual(shot['requirements']['duration_seconds_exact'], '3/1')
        for key in ('subject', 'action', 'shot_size', 'scene', 'direction', 'camera', 'composition'):
            self.assertEqual(shot['requirements'][key], job['analysis']['shots'][0][key])
        self.assertEqual(shot['request']['kind'], 'video')
        self.assertFalse(shot['requirements']['post_validation']['allow_freeze'])
        self.assertEqual(self.legacy.requests, [])

    def test_first_submission_persists_fingerprint_before_mutation(self):
        job = fixture_job()
        state = self.tick(job, 100)
        self.assertEqual(len(self.legacy.requests), 1)
        request = self.legacy.requests[0]
        self.assertRegex(request['request_fingerprint'], r'^[a-f0-9]{64}$')
        self.assertEqual(request['reference_context'], {'job_id': job['id'], 'shot_id': 'shot_1', 'attempt': 1})
        record = self.row(job)['task_records'][0]
        self.assertEqual(record['request_fingerprint'], request['request_fingerprint'])
        self.assertNotIn('pending_submission', self.row(job))
        self.assertEqual(state['state'], 'searching')
        plan = json.loads((self.service.directory(job['id']) / 'material-search-plan.json').read_text())
        self.assertEqual(state['skill_sha256'], plan['skill_sha256'])
        reloaded = self.service.reload()
        self.tick(reloaded, 103)
        self.assertEqual(len(self.legacy.requests), 1)

    def test_lost_response_recovers_same_task_after_reload_and_config_change(self):
        job = fixture_job()
        self.legacy.lose_next_response = True
        self.tick(job, 100)
        fingerprint = self.legacy.requests[0]['request_fingerprint']
        self.assertEqual(self.row(job)['status'], 'connection_error')
        self.assertEqual(self.row(job)['pending_submission']['request_fingerprint'], fingerprint)
        self.assertEqual(len(self.legacy.tasks), 1)
        self.legacy.agent_configured = False  # Pending identity may not change with configuration.
        reloaded = self.service.reload()
        self.tick(reloaded, 116)
        self.assertEqual(len(self.legacy.requests), 1)
        self.assertEqual(self.legacy.lookups, [fingerprint, fingerprint])
        self.assertEqual(self.row(reloaded)['task_records'][0]['task_id'], 101)
        self.assertEqual(self.row(reloaded)['task_records'][0]['request']['mode'], 'agent')
        self.assertNotIn('pending_submission', self.row(reloaded))

    def test_429_respects_largest_server_wait_and_resumes_same_task(self):
        job = fixture_job()
        self.tick(job, 100)
        self.legacy.tasks[101].update(state='failed', result={'network_retries': 0,
            'failure': {'category': 'rate_limited', 'cooldown_until': 140, 'retry_after_seconds': 50}})
        state = self.tick(job, 110)
        self.assertEqual(self.row(job)['status'], 'cooldown')
        self.assertEqual(state['next_retry_at'], 160)
        self.tick(self.service.reload(), 159)
        self.assertEqual(self.legacy.resumes, [])
        reloaded = self.service.reload()
        self.tick(reloaded, 160)
        self.assertEqual(self.legacy.resumes, [101])
        self.assertEqual(len(self.legacy.requests), 1)
        self.assertEqual([r['task_id'] for r in self.row(reloaded)['task_records']], [101])
        self.assertEqual(self.row(reloaded)['status'], 'searching')

    def test_shared_rate_limit_prevents_other_shot_submission(self):
        job = fixture_job(3)
        self.tick(job, 100)
        self.finish(102)
        self.legacy.tasks[101].update(state='failed', result={'errors': ['HTTP 429'],
            'failure': {'cooldown_until': 200}})
        self.tick(job, 110)
        self.assertEqual(len(self.legacy.requests), 2)
        self.assertEqual(self.row(job, 'shot_3')['status'], 'cooldown')
        self.assertEqual(self.row(job, 'shot_3')['next_retry_at'], 200)

    def test_archive_cooldown_does_not_block_new_commons_searches(self):
        job = fixture_job(3)
        self.tick(job, 100)
        self.finish(102)
        self.legacy.tasks[101].update(state='failed', result={'errors':['HTTP 429'],
            'failure':{'category':'rate_limit','source':'Internet Archive',
                       'cooldown_until':200,'retry_after_seconds':50,
                       'target':{'stage':'supplemental'}}})
        state = self.tick(job, 110)
        self.assertEqual(self.legacy.resumes, [])
        self.assertEqual(self.row(job,'shot_1')['status'],'cooldown')
        self.assertEqual(self.row(job,'shot_1')['next_retry_at'],200)
        self.assertEqual(self.row(job,'shot_3')['status'],'searching')
        self.assertTrue(any(r['reference_context']['shot_id']=='shot_3' for r in self.legacy.requests))
        self.assertEqual(len(self.legacy.requests),4)
        self.assertEqual(state['source_cooldowns']['Internet Archive'],200)

    def test_historical_commons_deadline_blocks_all_commons_even_after_retry_budget(self):
        job = fixture_job(3)
        # The newest historical task is done; an older task's source deadline
        # still applies even if that task itself has exhausted network retries.
        job['legacy_search_history'] = {'shot_1':[{'task_id':7},{'task_id':8}]}
        self.legacy.tasks[7] = {'id':7,'state':'failed','candidate_items':[],
            'result':{'network_retries':3,'errors':['HTTP 429'],
                      'failure':{'category':'rate_limit','source':'Wikimedia Commons',
                                 'cooldown_until':400,'retry_after_seconds':80,'occurred_at':320}}}
        self.legacy.tasks[8] = {'id':8,'state':'done','result':{},'candidate_items':[]}
        state = self.tick(job, 330)
        self.assertEqual(self.legacy.requests,[])
        self.assertEqual(self.legacy.resumes,[])
        for sid in ('shot_1','shot_2','shot_3'):
            self.assertEqual(self.row(job,sid)['status'],'cooldown')
            self.assertGreaterEqual(self.row(job,sid)['next_retry_at'],400)
        self.assertGreaterEqual(state['source_cooldowns']['Wikimedia Commons'],400)

    def test_archive_resume_obeys_its_own_wait_without_unrelated_commons_delay(self):
        job = fixture_job()
        self.tick(job,100)
        self.legacy.tasks[101].update(state='failed',result={'errors':['HTTP 429'],
            'failure':{'category':'rate_limit','source':'Internet Archive','cooldown_until':200,
                       'retry_after_seconds':50,'target':{'stage':'supplemental'}}})
        # This retained history must protect new Commons work, but not an
        # Archive-only resume whose Commons phase is already complete.
        job['legacy_search_history'] = {'shot_1':[{'task_id':7}]}
        self.legacy.tasks[7] = {'id':7,'state':'done','candidate_items':[],
            'result':{'last_failure':{'category':'rate_limit','source':'Wikimedia Commons',
                                     'cooldown_until':300,'retry_after_seconds':60,'occurred_at':240}}}
        self.tick(job,110)
        self.assertEqual(self.row(job)['next_retry_at'],200)
        self.tick(self.service.reload(),199)
        self.assertEqual(self.legacy.resumes,[])
        reloaded = self.service.reload()
        self.tick(reloaded,200)
        self.assertEqual(self.legacy.resumes,[101])
        self.assertEqual(len(self.legacy.requests),1)
        self.assertEqual(self.row(reloaded)['status'],'searching')

    def test_two_concurrent_tasks_and_two_total_tasks_per_shot(self):
        job = fixture_job(3)
        self.tick(job, 100)
        self.assertEqual(len(self.legacy.requests), 2)
        self.assertEqual(self.row(job, 'shot_3')['status'], 'queued')
        self.tick(job, 103)
        self.assertEqual(len(self.legacy.requests), 2)
        for tid in list(self.legacy.tasks):
            self.finish(tid)
        self.tick(job, 106)
        self.assertEqual(len(self.legacy.requests), 4)
        self.assertEqual(self.row(job, 'shot_3')['status'], 'queued')
        for tid in list(self.legacy.tasks):
            self.finish(tid)
        self.tick(job, 109)
        self.assertEqual(len(self.legacy.requests), 5)
        for sid in ('shot_1', 'shot_2'):
            self.assertEqual(self.row(job, sid)['status'], 'exhausted')
            self.assertEqual(len(self.row(job, sid)['task_records']), 2)
        self.finish(105)
        self.tick(job, 112)
        self.finish(106)
        self.row(job, 'shot_3')['retry_reason'] = 'previous cooldown'
        self.tick(job, 115)
        self.assertEqual(len(self.legacy.requests), 6)
        self.assertEqual(self.row(job, 'shot_3')['status'], 'exhausted')
        self.assertNotIn('retry_reason', self.row(job, 'shot_3'))
        for sid in ('shot_1', 'shot_2', 'shot_3'):
            submitted = [r for r in self.legacy.requests if r['reference_context']['shot_id'] == sid]
            self.assertEqual(len(submitted), 2)
            self.assertNotEqual(submitted[0]['keywords'], submitted[1]['keywords'])

    def test_ambiguous_submission_occupies_slot_in_same_scheduling_step(self):
        job = fixture_job(3)
        self.legacy.lose_next_response = True
        self.tick(job, 100)
        self.assertEqual(len(self.legacy.tasks), 2)
        self.assertEqual(len(self.legacy.requests), 2)
        self.assertIn('pending_submission', self.row(job, 'shot_1'))
        self.assertEqual(self.row(job, 'shot_3')['status'], 'queued')

    def test_network_retry_budget_never_becomes_a_new_search_attempt(self):
        job = fixture_job()
        self.tick(job, 100)
        self.legacy.tasks[101].update(state='failed', result={'network_retries': 3,
            'failure': {'category': 'network'}})
        self.tick(job, 500)
        self.assertEqual(self.row(job)['status'], 'failed')
        self.assertEqual(self.legacy.resumes, [])
        self.assertEqual(len(self.legacy.requests), 1)
        self.assertEqual(self.row(job)['attempts'], 1)

    def test_new_task_network_budget_does_not_inherit_old_job_wait(self):
        job = fixture_job()
        self.tick(job, 100)
        self.legacy.tasks[101].update(state='failed', result={'failure':{'category':'network'}})
        self.tick(job, 10000)
        self.assertEqual(self.row(job)['status'], 'cooldown')
        self.assertEqual(self.row(job)['task_records'][0]['network_wait_started_at'], 10000)
        self.tick(job, 10015)
        self.assertEqual(self.legacy.resumes, [101])

    def test_all_historical_tasks_and_candidates_survive_migration(self):
        job = fixture_job()
        records = [{'task_id': i, 'request': {'keywords': 'old query %s' % i}} for i in (7, 8, 9)]
        job['material_search'] = {'schema': 'reference-material-search/v1', 'started_at': 90,
            'shots': {'shot_1': {'task_records': [records[0]]}}}
        job['legacy_search_history'] = {'shot_1': [records[0], records[1]]}
        job['legacy_search_tasks'] = {'shot_1': records[2]}
        for tid in (7, 8, 9):
            self.legacy.tasks[tid] = {'id': tid, 'state': 'done', 'result': {},
                'candidates': [{'asset': 'asset_%d' % tid}],
                'candidate_items': [{'id': tid, 'asset_id': 'asset_%d' % tid, 'approved': False, 'downloaded': False}]}
        self.tick(job, 100)
        self.assertEqual({r['task_id'] for r in self.row(job)['task_records']}, {7, 8, 9, 101})
        self.assertEqual({c['id'] for c in self.row(job)['candidate_items']}, {7, 8, 9})
        self.assertEqual({c['id'] for c in search.candidate_items(job, 'shot_1', PROJECT)}, {7, 8, 9})
        self.assertEqual(search.related_asset_ids(job, 'shot_1', PROJECT), {'asset_7', 'asset_8', 'asset_9'})
        self.assertEqual(len(self.legacy.requests), 1)
        self.assertEqual(self.row(job)['plan_attempts'], 1)

    def test_approved_candidate_rejected_by_match_triggers_second_query(self):
        job = fixture_job()
        self.tick(job, 100)
        candidate = {'id': 11, 'asset_id': 'asset_a', 'downloaded': True, 'approved': True}
        self.finish(101, candidate_items=[candidate], candidates=[{'asset': 'asset_a'}])
        self.legacy.exported = [{'id': 'exported_clip_a', 'legacy_asset_id': 'asset_a'}]
        self.tick(job, 103)
        self.assertEqual(self.row(job)['status'], 'ready_to_match')
        self.assertEqual(len(self.legacy.requests), 1)
        job['rejected']['shot_1'] = ['exported_clip_a']
        self.tick(job, 106)
        self.assertEqual(len(self.legacy.requests), 2)
        self.assertEqual(self.row(job)['status'], 'searching')
        self.assertNotEqual(self.legacy.requests[0]['keywords'], self.legacy.requests[1]['keywords'])
        self.assertEqual(self.row(job)['candidate_items'], [candidate])
        self.assertEqual(self.legacy.export_calls[-1], {'asset_a'})

    def test_downloaded_unreviewed_candidate_waits_without_claiming_match(self):
        job = fixture_job()
        self.tick(job, 100)
        self.finish(101, candidate_items=[{'id': 11, 'asset_id': 'asset_a', 'downloaded': True, 'approved': False}])
        self.tick(job, 103)
        self.assertEqual(self.row(job)['status'], 'content_review')
        self.assertEqual(len(self.legacy.requests), 1)
        self.assertNotIn('matches', job)

    def test_incomplete_old_task_does_not_consume_the_only_full_query(self):
        job = fixture_job()
        job['analysis']['shots'][0]['queries'] = ['train passing']
        job['legacy_search_tasks'] = {'shot_1': {'task_id': 7}}
        self.legacy.tasks[7] = {'id':7, 'state':'done', 'result':{}, 'candidates':[], 'candidate_items':[]}
        self.tick(job, 100)
        self.assertEqual(len(self.legacy.requests), 1)
        self.assertEqual(self.legacy.requests[0]['keywords'], 'train passing')
        self.assertEqual(self.row(job)['attempts'], 2)
        self.assertEqual(self.row(job)['plan_attempts'], 1)

    def test_downloaded_content_rejection_allows_next_query_without_approval(self):
        job = fixture_job()
        self.tick(job, 100)
        self.finish(101, candidate_items=[{'id':11,'asset_id':'asset_a', 'kind':'video', 'downloaded':True,'approved':False}])
        job['material_search']['content_reviews'] = {'shot_1': {'asset_a': {'status':'rejected', 'reason':'wrong action'}}}
        self.tick(job, 103)
        self.assertEqual(len(self.legacy.requests), 2)
        self.assertNotIn('matches', job)

    def test_lost_response_reconciles_before_changed_query_gate(self):
        job = fixture_job()
        self.tick(job, 100)
        self.finish(101)
        self.legacy.lose_next_response = True
        self.tick(job, 103)
        job['analysis']['shots'][0]['queries'] = ['train passing medium platform']
        self.tick(job, 120)
        self.assertEqual(len(self.legacy.requests), 2)
        self.assertEqual(len(self.row(job)['task_records']), 2)
        self.assertNotIn('pending_submission', self.row(job))

    def test_exhausted_content_review_does_not_permanently_stop_search(self):
        for result in ({'status':'error','error_count':2}, {'status':'deferred'}):
            job = fixture_job()
            self.legacy.tasks.clear(); self.legacy.requests.clear(); self.legacy.by_fingerprint.clear()
            self.tick(job, 100)
            tid = self.row(job)['task_records'][-1]['task_id']
            self.finish(tid, candidate_items=[{'id':11,'asset_id':'asset_a','kind':'video','downloaded':True,'approved':False}])
            job['material_search']['content_reviews'] = {'shot_1':{'asset_a':result}}
            self.tick(job, 103)
            self.assertEqual(len(self.legacy.requests), 2)
            self.assertIn('未确认', self.row(job)['review_warning'])
            self.assertNotIn('matches', job)

    def test_unreachable_existing_task_never_authorizes_replacement_submit(self):
        job = fixture_job()
        self.tick(job, 100)
        self.legacy.task_errors.add(101)
        self.tick(job, 116)
        self.tick(self.service.reload(), 132)
        self.assertEqual(len(self.legacy.requests), 1)
        self.assertEqual(self.legacy.resumes, [])
        self.assertEqual(self.row(self.service.reload())['status'], 'connection_error')
        self.assertEqual(self.row(self.service.reload())['task_records'][0]['task_id'], 101)

    def test_failed_reconciliation_never_blindly_submits(self):
        job = fixture_job()
        self.legacy.find_error = True
        self.tick(job, 100)
        self.tick(self.service.reload(), 116)
        self.assertEqual(self.legacy.requests, [])
        self.assertEqual(len(set(self.legacy.lookups)), 1)
        self.legacy.find_error = False
        self.tick(self.service.reload(), 132)
        self.assertEqual(len(self.legacy.requests), 1)

    def test_permanent_request_error_is_not_replayed_every_tick(self):
        job = fixture_job()
        error = self.legacy.LegacyError('Fixture: unsupported permanent request value', retryable=False)
        with patch.object(self.legacy, 'search', side_effect=error) as submit:
            self.tick(job, 100)
            self.tick(self.service.reload(), 116)
            self.tick(self.service.reload(), 132)
        submit.assert_called_once()
        self.assertEqual(self.row(self.service.reload())['status'], 'blocked')
        self.assertNotIn('pending_submission', self.row(self.service.reload()))

    def test_unavailable_service_retains_job_without_submission(self):
        job = fixture_job()
        self.legacy.service_error = True
        state = self.tick(job, 100)
        self.assertEqual(state['state'], 'connection_error')
        self.assertEqual(state['next_retry_at'], 115)
        self.assertEqual(self.legacy.requests, [])
        self.assertTrue((self.service.directory(job['id']) / 'job.json').exists())

    def test_missing_semantics_blocks_and_can_resume_after_correction(self):
        job = fixture_job()
        del job['analysis']['shots'][0]['direction']
        self.tick(job, 100)
        self.assertEqual(self.row(job)['status'], 'blocked')
        self.assertIn('direction', self.row(job)['reason'])
        self.assertEqual(self.legacy.requests, [])
        job['analysis']['shots'][0]['direction'] = 'left to right'
        self.tick(job, 103)
        self.assertEqual(len(self.legacy.requests), 1)


if __name__ == '__main__':
    unittest.main()
