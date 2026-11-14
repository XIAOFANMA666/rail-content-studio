"""Offline worker tests: source recovery, retained leads, and no model replay."""
from contextlib import closing
from email.message import Message
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import commons
import http_transport
import search_agent
import search_recovery


class SearchFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.file = self.folder / 'test.sqlite'
        with closing(self.db()) as conn:
            conn.executescript(commons.SCHEMA)
        for target in ('http_transport.read', 'http_transport.subprocess.run'):
            guard = patch(target, side_effect=AssertionError('offline test attempted network/process'))
            guard.start()
            self.addCleanup(guard.stop)
        self.body = dict(mode='agent',queries=['first filetype:video'],videoOnly=True,
                         supplementSources=True,search={},script='train',rules=[])
        self.counts = dict(found=0,downloaded=0,lead=0,duplicate=0,errors=[],pageIds=[],seen=[],cursor=0)

    def db(self):
        conn = sqlite3.connect(self.file)
        conn.row_factory = sqlite3.Row
        return conn

    def insert(self, counts=None, body=None, state='running'):
        with closing(self.db()) as conn, conn:
            return conn.execute('INSERT INTO commons_job(payload,result,state) VALUES (?,?,?)',
                (json.dumps(body or self.body),json.dumps(counts or self.counts),state)).lastrowid

    def row(self, job_id):
        with closing(self.db()) as conn:
            row = dict(conn.execute('SELECT * FROM commons_job WHERE id=?',(job_id,)).fetchone())
        row['result'] = json.loads(row['result'])
        return row

    def test_agent_recovers_exact_round_with_archive_leads_and_no_model_call(self):
        counts = {**self.counts, 'found':6,'lead':6,'seen':[-1,-2,-3,-4,-5,-6],
                  'pageIds':[-1,-2,-3,-4,-5,-6], 'resume_target':{'stage':'commons','round_index':1},
                  'startedAt':1,'supplementStartedAt':1,
                  'rounds':[{'action':'search','query':'completed','complete':True,'pageIds':[]},
                            {'action':'search','query':'failed exact query','complete':False,'pageIds':[]}],
                  'supplemental':{'complete':True}}
        job_id = self.insert(counts)
        page = {'pageid':101,'imageinfo':[{}]}
        with patch.object(search_agent,'settings',return_value=None), \
             patch.object(search_agent,'next_action',side_effect=AssertionError('model must not replay')), \
             patch.object(commons,'collect',return_value=[page]) as collect, \
             patch.object(commons,'evidence',return_value={'title':'train'}), \
             patch.object(commons,'assess',return_value=('',[],[])), \
             patch.object(commons,'queue_candidate',return_value='lead'), \
             patch.object(commons,'candidate_gaps',return_value=(['needs review'],[])):
            commons.run_agent_job(self.db,self.folder,job_id,self.body,counts)
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[0],'failed exact query')
        result = self.row(job_id)
        self.assertEqual(result['state'],'done')
        self.assertEqual(result['result']['pageIds'],[-1,-2,-3,-4,-5,-6,101])
        self.assertEqual(len(result['result']['rounds']),2)
        self.assertTrue(result['result']['rounds'][0]['complete'])
        self.assertTrue(result['result']['rounds'][1]['complete'])
        self.assertNotIn('resume_target',result['result'])

    def test_agent_429_preserves_retry_after_and_unfinished_source_round(self):
        headers = Message()
        headers['Retry-After'] = '180'
        exc = urllib.error.HTTPError('https://commons.wikimedia.org/',429,'limited',headers,None)
        counts = {**self.counts,'supplemental':{'complete':True}}
        job_id = self.insert(counts)
        with patch.object(search_agent,'settings',return_value=None), \
             patch.object(commons,'collect',side_effect=exc), \
             patch.object(commons,'candidate_gaps',return_value=(['missing'],[])):
            commons.run_agent_job(self.db,self.folder,job_id,self.body,counts)
        result = self.row(job_id)
        self.assertEqual(result['state'],'failed')
        self.assertFalse(result['result']['rounds'][0]['complete'])
        self.assertEqual(result['result']['failure']['category'],'rate_limit')
        self.assertEqual(result['result']['retry_after_seconds'],180)
        self.assertEqual(result['result']['failure']['target'],{'stage':'commons','round_index':0})

    def test_fixed_network_failure_retries_same_query_with_retained_results(self):
        body = {**self.body,'mode':'fixed','queries':['completed','failed query','later query']}
        counts = {**self.counts,'cursor':1,'found':4,'lead':4,'seen':[-1,-2,-3,-4],
                  'pageIds':[-1,-2,-3,-4],'supplemental':{'complete':True}}
        job_id = self.insert(counts,body)
        with patch.object(commons,'collect',side_effect=TimeoutError('网络请求超时')) as collect, \
             patch.object(commons,'candidate_gaps',return_value=(['missing'],[])):
            commons.run_fixed_job(self.db,self.folder,job_id,body,counts)
        self.assertEqual(collect.call_args.args[0],'failed query')
        result = self.row(job_id)['result']
        self.assertEqual(result['cursor'],1)
        self.assertEqual(result['failure']['target'],{'stage':'commons','query_index':1})
        with closing(self.db()) as conn, conn:
            resumed = search_recovery.resume_search(conn,job_id,now=result['cooldown_until']+1)
        self.assertEqual(resumed['id'],job_id)
        counts = self.row(job_id)['result']
        with patch.object(commons,'collect',return_value=[]) as collect, \
             patch.object(commons,'candidate_gaps',return_value=(['missing'],[])):
            commons.run_fixed_job(self.db,self.folder,job_id,body,counts)
        collect.assert_called_once_with('failed query')
        result = self.row(job_id)
        self.assertEqual(result['state'],'done')
        self.assertEqual(result['result']['cursor'],2)
        self.assertEqual(result['result']['pageIds'],[-1,-2,-3,-4])
        self.assertEqual(len(result['result']['error_history']),1)

    def test_source_cooldown_pauses_other_queued_tasks(self):
        counts = dict(self.counts)
        search_recovery.record_failure(counts,urllib.error.HTTPError('',429,'limited',None,None),
                                       target={'stage':'commons','query_index':0},now=100)
        self.insert(counts,state='failed')
        queued = self.insert(state='queued')
        with closing(self.db()) as conn:
            self.assertIsNone(search_recovery.next_queued(conn,now=159))
            self.assertEqual(search_recovery.next_queued(conn,now=160)['id'],queued)

    def test_agent_can_adjust_after_four_duplicates_and_stops_after_three_rounds(self):
        counts = {**self.counts,'supplemental':{'complete':True}}
        job_id = self.insert(counts)
        batches = [[{'pageid':100*index+j,'imageinfo':[{}]} for j in range(1,5)] for index in (1,2,3)]
        actions = [dict(action='search',query=query,reason='refine full subject and action',
                        planner_called=True,planner_status='completed') for query in ('second query','third query')]
        with patch.object(search_agent,'settings',return_value=None), \
             patch.object(search_agent,'next_action',side_effect=actions) as planner, \
             patch.object(commons,'collect',side_effect=batches) as collect, \
             patch.object(commons,'evidence',return_value={'title':'possible video'}), \
             patch.object(commons,'assess',return_value=('',[],['visual content unknown'])), \
             patch.object(commons,'queue_candidate',return_value='duplicate'), \
             patch.object(commons,'candidate_gaps',return_value=(['no verified reference match'],[])):
            commons.run_agent_job(self.db,self.folder,job_id,self.body,counts)
        self.assertEqual(collect.call_count,3)
        self.assertEqual(planner.call_count,2)
        self.assertEqual(len(counts['rounds']),3)
        self.assertEqual(counts['duplicate'],12)
        self.assertEqual(len(counts['planning']),2)
        self.assertTrue(all(event['called'] for event in counts['planning']))
        self.assertIn('上限',counts['stopReason'])

    def test_agent_download_budget_keeps_additional_candidates_as_leads(self):
        counts = {**self.counts,'supplemental':{'complete':True}}
        job_id = self.insert(counts)
        batches = [[{'pageid':100*index+j,'imageinfo':[{}]} for j in range(1,5)] for index in (1,2,3)]
        def queue(*args,hold_reason='',**kwargs):
            return 'lead' if hold_reason else 'downloaded'
        with patch.object(search_agent,'settings',return_value=None), \
             patch.object(search_agent,'next_action',side_effect=[
                 {'action':'search','query':'second','reason':'refine'},
                 {'action':'search','query':'third','reason':'refine'}]), \
             patch.object(commons,'collect',side_effect=batches), \
             patch.object(commons,'evidence',return_value={'title':'possible video'}), \
             patch.object(commons,'assess',return_value=('',[],['visual content unknown'])), \
             patch.object(commons,'queue_candidate',side_effect=queue), \
             patch.object(commons,'candidate_gaps',return_value=(['no verified reference match'],[])):
            commons.run_agent_job(self.db,self.folder,job_id,self.body,counts)
        self.assertEqual(counts['downloaded'],5)
        self.assertEqual(counts['lead'],7)
        self.assertEqual(len(counts['pageIds']),12)
        self.assertLessEqual(len(counts['pageIds']),commons.MAX_AGENT_CANDIDATES)

    def test_archive_uses_latest_query_but_network_resume_keeps_original_query(self):
        body = {**self.body,'anchor':'Wymondham','external_request_id':'a'*64,
                'reference_context':{'job_id':'reference','shot_id':'s01'}}
        for recovering in (False,True):
            counts = {**self.counts,'errors':[], 'rounds':[
                {'action':'search','query':'woman coffee filetype:video','complete':True},
                {'action':'search','query':'woman suitcase stairs filetype:video','complete':True}]}
            if recovering:
                counts.update(resume_target={'stage':'supplemental'},supplemental={
                    'query':'original failed archive query','returned':0,'added':0,'complete':False})
            job_id = self.insert(counts,body)
            with patch.object(commons,'candidate_gaps',return_value=(['missing'],[])), \
                 patch.object(commons.video_sources,'archive_search',return_value=[]) as archive:
                commons.finish_video_job(self.db,self.folder,body,job_id,counts)
            expected = 'original failed archive query' if recovering else 'woman suitcase stairs'
            self.assertEqual(archive.call_args.args[0],expected)
            self.assertEqual(counts['supplemental']['query'],expected)

    def test_archive_failure_is_recoverable_and_obeys_existing_source_cooldown(self):
        counts = {**self.counts,'network_retries':1}
        job_id = self.insert(counts)
        with patch.object(commons,'candidate_gaps',return_value=(['missing'],[])), \
             patch.object(commons.video_sources,'archive_search',side_effect=TimeoutError('网络请求超时')) as archive:
            commons.finish_video_job(self.db,self.folder,self.body,job_id,counts)
        archive.assert_called_once()
        self.assertEqual(counts['failure']['source'],'Internet Archive')
        self.assertEqual(counts['failure']['target'],{'stage':'supplemental'})
        self.assertEqual(counts['failure']['retry_after_seconds'],120)
        original_until = counts['failure']['cooldown_until']
        second = dict(self.counts,errors=[])
        next_id = self.insert(second)
        with patch.object(commons,'candidate_gaps',return_value=(['missing'],[])), \
             patch.object(commons.video_sources,'archive_search') as archive:
            commons.finish_video_job(self.db,self.folder,self.body,next_id,second)
        archive.assert_not_called()
        self.assertEqual(second['failure']['cooldown_until'],original_until)
        self.assertFalse(second['supplemental']['complete'])


if __name__ == '__main__':
    unittest.main()
