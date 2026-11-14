"""Legacy bridge tests; external HTTP calls are mocked, local video export is real."""
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

import reference_legacy_media as bridge
import reference_video as ref


class LegacyBridgeTests(unittest.TestCase):
    def test_config_is_read_only_and_never_starts_or_contacts_service(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(bridge, '_request') as request, patch.object(bridge.subprocess, 'Popen') as spawn:
            directory = Path(temp)
            value = bridge.config(directory/'rail')
            self.assertFalse(value['available'])
            self.assertEqual(list(directory.iterdir()), [])
            request.assert_not_called(); spawn.assert_not_called()

    def test_health_checks_structure_and_only_returns_public_fields(self):
        for invalid in ({}, [], {'configured':1}, {'configured':True}, {'configured':False}):
            with self.subTest(invalid=invalid), patch.object(bridge, '_request', return_value=invalid):
                self.assertIsNone(bridge._health(.1))
        with patch.object(bridge, '_request', return_value={
                'configured':True,'provider':'fixture','model':'fixture','api_key':'must-not-escape'}):
            self.assertEqual(bridge._health(.1), {'configured':True,'provider':'fixture','model':'fixture'})

    def test_healthy_service_does_not_start_or_create_logs(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(bridge, '_health', return_value={'configured':False,'message':'not configured'}), \
             patch.object(bridge.subprocess, 'Popen') as spawn:
            root = Path(temp)
            value = bridge.ensure_running(root/'rail', root/'out')
            self.assertTrue(value['running']);self.assertFalse(value['started'])
            self.assertFalse((root/'out').exists());spawn.assert_not_called()

    def test_start_uses_current_python_and_isolates_log(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp);old = root/'media-library';old.mkdir();(old/'server.py').write_text('')
            process = Mock(pid=12345);process.poll.return_value=None
            with patch.object(bridge.tempfile, 'gettempdir', return_value=temp), \
                 patch.object(bridge, '_health', side_effect=[None,None,{'configured':False,'message':'fixture'}]), \
                 patch.object(bridge, '_port_open', return_value=False), \
                 patch.object(bridge, '_pid_running', return_value=False), \
                 patch.object(bridge.subprocess, 'Popen', return_value=process) as spawn:
                value = bridge.ensure_running(root/'rail', root/'out')
            self.assertTrue(value['started'])
            self.assertEqual(spawn.call_args.args[0], [bridge.sys.executable,'-B',str(old.resolve()/'server.py'),'--port','8765'])
            self.assertEqual(spawn.call_args.kwargs['cwd'], old.resolve())
            self.assertTrue((root/'out/legacy-media-server.log').is_file())

    def test_wrong_service_on_port_is_never_replaced(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old=root/'media-library';old.mkdir();(old/'server.py').write_text('')
            with patch.object(bridge.tempfile,'gettempdir',return_value=temp), \
                 patch.object(bridge,'_health',return_value=None),patch.object(bridge,'_port_open',return_value=True), \
                 patch.object(bridge.subprocess,'Popen') as spawn:
                with self.assertRaisesRegex(bridge.LegacyError,'端口'):
                    bridge.ensure_running(root/'rail',root/'out')
                spawn.assert_not_called()

    def test_concurrent_start_spawns_only_once(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old=root/'media-library';old.mkdir();(old/'server.py').write_text('')
            ready=threading.Event();process=Mock(pid=23456);process.poll.return_value=None
            def launch(*args,**kwargs):
                ready.set();return process
            def health(timeout):
                return {'configured':False,'message':'fixture'} if ready.is_set() else None
            with patch.object(bridge.tempfile,'gettempdir',return_value=temp), \
                 patch.object(bridge,'_health',side_effect=health),patch.object(bridge,'_port_open',return_value=False), \
                 patch.object(bridge,'_pid_running',return_value=False), \
                 patch.object(bridge.subprocess,'Popen',side_effect=launch) as spawn, ThreadPoolExecutor(2) as pool:
                futures=[pool.submit(bridge.ensure_running,root/'rail',root/('out'+str(i))) for i in range(2)]
                values=[future.result() for future in futures]
            self.assertEqual(spawn.call_count,1)
            self.assertEqual(sum(value['started'] for value in values),1)

    def test_start_deadline_and_slow_process_do_not_spawn_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old=root/'media-library';old.mkdir();(old/'server.py').write_text('')
            clock=[0.0];process=Mock(pid=34567);process.poll.return_value=None
            def sleep(seconds):clock[0]+=seconds
            with patch.object(bridge.tempfile,'gettempdir',return_value=temp), \
                 patch.object(bridge.time,'monotonic',side_effect=lambda:clock[0]), \
                 patch.object(bridge.time,'sleep',side_effect=sleep), \
                 patch.object(bridge,'_health',return_value=None),patch.object(bridge,'_port_open',return_value=False), \
                 patch.object(bridge,'_pid_running',side_effect=lambda pid:pid==34567), \
                 patch.object(bridge.subprocess,'Popen',return_value=process) as spawn:
                for _ in range(2):
                    before=clock[0]
                    with self.assertRaisesRegex(bridge.LegacyError,'5 秒'):
                        bridge.ensure_running(root/'rail',root/'out')
                    self.assertLessEqual(clock[0]-before,5.0001)
                self.assertEqual(spawn.call_count,1)

    def test_start_failure_does_not_echo_exception(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old=root/'media-library';old.mkdir();(old/'server.py').write_text('')
            with patch.object(bridge.tempfile,'gettempdir',return_value=temp), \
                 patch.object(bridge,'_health',return_value=None),patch.object(bridge,'_port_open',return_value=False), \
                 patch.object(bridge,'_pid_running',return_value=False), \
                 patch.object(bridge.subprocess,'Popen',side_effect=OSError('private key fixture')):
                with self.assertRaises(bridge.LegacyError) as error:
                    bridge.ensure_running(root/'rail',root/'out')
                self.assertNotIn('private key',str(error.exception))

    def test_queue_full_is_retryable_not_reported_as_empty_results(self):
        body=json.dumps({'error':'已有 5 个任务在处理，请稍后再试'},ensure_ascii=False).encode()
        error=urllib.error.HTTPError('http://localhost:8765',400,'bad',{},io.BytesIO(body))
        with patch.object(bridge.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(bridge.LegacyBusy) as raised:
                bridge._request('/api/commons/start',{'script':'train'})
        self.assertTrue(raised.exception.retryable)

    def test_resume_search_uses_existing_job_and_reports_reuse(self):
        for reused,state in ((False,'queued'),(True,'running')):
            with self.subTest(reused=reused),patch.object(bridge,'_request',return_value={
                    'id':17,'state':state,'reused':reused,'network_retries':2}) as request:
                result=bridge.resume_search(17)
            request.assert_called_once_with('/api/commons/resume-search',{'id':17},base_url=bridge.DEFAULT_URL)
            self.assertEqual(result,{'provider':'legacy-commons','task_id':17,'state':state,
                                    'reused':reused,'network_retries':2,'resumed':not reused})

    def test_resume_rejects_invalid_id_or_mismatched_response(self):
        for value in (0,-1,True,'17'):
            with self.subTest(value=value),patch.object(bridge,'_request') as request:
                with self.assertRaises(bridge.LegacyError):bridge.resume_search(value)
                request.assert_not_called()
        for response in ({'id':18,'state':'queued','reused':False,'network_retries':1},
                         {'id':17,'state':'failed','reused':False,'network_retries':1},
                         {'id':17,'state':'queued','reused':False,'network_retries':True}):
            with self.subTest(response=response),patch.object(bridge,'_request',return_value=response):
                with self.assertRaises(bridge.LegacyError):bridge.resume_search(17)

    def test_resume_cooldown_retains_safe_scheduling_fields(self):
        body=json.dumps({'error':'private-key must not escape','id':17,'retry_after_seconds':73,
                         'cooldown_until':1900000000,'api_key':'private-key'}).encode()
        error=urllib.error.HTTPError('http://localhost:8765',429,'private-key',{},io.BytesIO(body))
        with patch.object(bridge.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(bridge.LegacyError) as raised:bridge.resume_search(17)
        exc=raised.exception
        self.assertEqual(exc.http_status,429);self.assertEqual(exc.status_code,429)
        self.assertEqual(exc.retry_after_seconds,73);self.assertEqual(exc.cooldown_until,1900000000)
        self.assertEqual(exc.task_id,17);self.assertTrue(exc.retryable)
        self.assertNotIn('private-key',str(exc));self.assertNotIn('private-key',str(exc.__dict__))

    def test_malformed_cooldown_metadata_and_http_400_are_not_leaked(self):
        body=json.dumps({'error':'sensitive detail','id':'17','retry_after_seconds':float('nan'),
                         'cooldown_until':True}).encode()
        error=urllib.error.HTTPError('http://localhost:8765',400,'bad',{},io.BytesIO(body))
        with patch.object(bridge.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=error
            with self.assertRaises(bridge.LegacyError) as raised:bridge.resume_search(17)
        exc=raised.exception
        self.assertFalse(exc.retryable);self.assertIsNone(exc.retry_after_seconds)
        self.assertIsNone(exc.cooldown_until);self.assertIsNone(exc.task_id)
        self.assertNotIn('sensitive detail',str(exc))

    def test_network_failure_is_retryable_without_echoing_cause(self):
        with patch.object(bridge.urllib.request,'build_opener') as opener:
            opener.return_value.open.side_effect=urllib.error.URLError('private-key')
            with self.assertRaises(bridge.LegacyError) as raised:bridge.resume_search(17)
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn('private-key',str(raised.exception))

    def test_video_search_uses_existing_queue_and_fixed_mode(self):
        with patch.object(bridge, '_request', return_value={'id': 8}) as request:
            result = bridge.search({'query': '火车中景驶过', 'kind': 'video', 'duration': 3, 'orientation': 'portrait'})
        self.assertEqual(result['task_id'], 8)
        path, body = request.call_args.args
        self.assertEqual(path, '/api/commons/start')
        self.assertTrue(body['videoOnly']);self.assertEqual(body['seconds'], 3)
        self.assertEqual(body['mode'], 'fixed');self.assertEqual(body['orientation'], '竖屏')
        self.assertTrue(body['direct'])

    def test_search_forwards_fingerprint_and_bounded_context(self):
        with patch.object(bridge,'_request',return_value={'id':8,'reused':True}) as request:
            value=bridge.search({'query':'train','request_fingerprint':'A'*64,
                'reference_context':{'job_id':'ref_001','shot_id':'shot-2','attempt':2,'api_key':'discard'}})
        body=request.call_args.args[1]
        self.assertEqual(body['request_fingerprint'],'a'*64)
        self.assertEqual(body['reference_context'],{'job_id':'ref_001','shot_id':'shot-2','attempt':2})
        self.assertTrue(value['reused'])
        for bad in ({'request_fingerprint':'token'}, {'reference_context':{'job_id':'../../file'}},
                    {'reference_context':{'attempt':True}}, {'reference_context':{'shot_id':'x'*129}}):
            with self.subTest(bad=bad),self.assertRaises(bridge.LegacyError):
                bridge.search({'query':'train',**bad})

    def test_full_constraints_are_preserved_and_keywords_are_marked_as_abbreviated(self):
        query='场景内容'+('q'*996);place='p'*500;required='\n'.join(['r'*399]*10)+'\n'
        self.assertEqual(len(required),4000)
        with patch.object(bridge,'_request',return_value={'id':18}) as request:
            result=bridge.search({'query':query,'place':place,'required':required})
        body=request.call_args.args[1]
        self.assertEqual(body['script'],query);self.assertEqual(body['location'],place)
        self.assertEqual(body['required'],required);self.assertEqual(len(body['keywords']),180)
        self.assertTrue(result['keywords_shortened'])
        with patch.object(bridge,'_request',return_value={'id':19}) as request:
            result=bridge.search({'query':query,'kind':'image','required':required})
        body=request.call_args.args[1]
        self.assertEqual(body['script'],query);self.assertEqual(body['required'],required)
        self.assertTrue(body['keywords'].endswith(' filetype:bitmap'))
        self.assertEqual(len(body['keywords']),180);self.assertTrue(result['keywords_shortened'])

    def test_oversize_constraints_fail_once_without_submission_or_truncation(self):
        for excess in ({'query':'q'*1001},{'place':'p'*501},{'required':'r'*4001}):
            with self.subTest(excess=list(excess)),patch.object(bridge,'_request') as request:
                with self.assertRaises(bridge.LegacyError) as raised:
                    bridge.search({'query':'train',**excess})
                self.assertFalse(raised.exception.retryable);self.assertEqual(raised.exception.http_status,400)
                request.assert_not_called()
        with patch.object(bridge.urllib.request,'build_opener') as opener:
            with self.assertRaises(bridge.LegacyError) as raised:
                bridge._request('/api/commons/start',{'script':'x'*65536})
            self.assertFalse(raised.exception.retryable);opener.assert_not_called()

    def test_image_search_and_motion_duration_are_distinct(self):
        with patch.object(bridge, '_request', return_value={'id': 9}) as request:
            bridge.search({'query': '火车站', 'kind': 'image', 'mode': 'agent'})
        body = request.call_args.args[1]
        self.assertFalse(body['videoOnly']);self.assertEqual(body['seconds'], 0)
        self.assertIn('filetype:bitmap', body['keywords']);self.assertEqual(body['mode'], 'fixed')
        with self.assertRaises(bridge.LegacyError):
            bridge.search({'query': '火车', 'kind': 'image', 'duration': 2})

    def test_endpoint_is_local_and_invalid_parameters_fail(self):
        for url in ('https://example.com', 'http://10.0.0.1:8765', 'http://user@localhost:8765', 'http://localhost:8765/api'):
            with self.subTest(url=url),self.assertRaises(bridge.LegacyError):bridge._base_url(url)
        self.assertEqual(bridge._base_url('http://127.0.0.1:8765/'),'http://127.0.0.1:8765')
        for request in ({'query':'x','duration':float('nan')},{'query':'x','kind':'other'},{'query':''}):
            with self.assertRaises(bridge.LegacyError):bridge.search(request)

    def test_task_includes_duplicates_linked_from_previous_job(self):
        with patch.object(bridge, '_request', return_value={
            'jobs':[{'id':8,'state':'done','result':{'pageIds':[11,22]}}],
            'candidates':[{'page_id':11,'job_id':1},{'page_id':22,'job_id':8},{'page_id':33,'job_id':7}]}):
            result=bridge.task(8)
        self.assertEqual([c['page_id'] for c in result['candidates']],[11,22])

    def test_image_task_filters_actual_mime(self):
        with patch.object(bridge, '_request', return_value={
            'jobs':[{'id':8,'state':'done','result':{}}],
            'candidates':[{'page_id':11,'job_id':8,'payload':{'mime':'image/jpeg'}},
                          {'page_id':22,'job_id':8,'payload':{'mime':'video/mp4'}}]}):
            result=bridge.task(8,kind='image')
        self.assertEqual([c['page_id'] for c in result['candidates']],[11])

    def fixture(self, directory, video=None):
        old=directory/'media-library';(old/'data/media').mkdir(parents=True)
        db=old/'data/library.sqlite3'
        data=video or b'fixture-video'
        digest=hashlib.sha256(data).hexdigest()
        folder=old/'data/media'/digest;folder.mkdir();(folder/'original.mp4').write_bytes(data)
        with sqlite3.connect(db) as c:
            c.executescript('CREATE TABLE asset(id TEXT,name TEXT,extension TEXT,info TEXT,status TEXT);'
                'CREATE TABLE segment(id INTEGER,asset TEXT,payload TEXT,state TEXT,reviewer TEXT,updated TEXT);')
            c.execute('INSERT INTO asset VALUES(?,?,?,?,?)',(digest,'fixture.mp4','.mp4',json.dumps({'duration':2}),'ready'))
            payload={'authorizationVerified':True,'publicationApproved':False,'license':'Fixture only',
                     'usage':'internal test','start':.5,'end':1.5,'description':'Fixture','tags':['test']}
            c.execute('INSERT INTO segment VALUES(?,?,?,?,?,?)',(1,digest,json.dumps(payload),'approved','Fixture reviewer','test'))
        return old,db,digest

    def job_fixture(self, directory, *, fingerprint='a'*64, result=None):
        old,db,digest=self.fixture(directory)
        payload={'external_request_id':fingerprint,'purpose':'internal','videoOnly':True}
        with sqlite3.connect(db) as c:
            c.executescript('CREATE TABLE commons_job(id INTEGER,payload TEXT,state TEXT,result TEXT);'
                'CREATE TABLE commons_candidate(page_id INTEGER,job_id INTEGER,payload TEXT,asset TEXT,note TEXT,state TEXT);')
            c.execute('INSERT INTO commons_job VALUES(?,?,?,?)',(1,json.dumps(payload),'done',json.dumps(result or {})))
            c.execute('INSERT INTO commons_candidate VALUES(?,?,?,?,?,?)',(11,1,
                json.dumps({'title':'Train','mime':'video/mp4','discovery':'https://example.com/train','mediaUrl':'https://example.com/train.mp4'}),digest,'','hold'))
        return old,db,digest

    def test_candidate_items_use_database_approval_and_keep_web_leads_unavailable(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.job_fixture(root)
            with sqlite3.connect(db) as c:
                c.execute('INSERT INTO commons_candidate VALUES(?,?,?,?,?,?)',(12,1,
                    json.dumps({'title':'Archive page','mime':'video/unknown','discovery':'https://archive.org/details/example'}),digest,'','hold'))
            before=db.read_bytes()
            value=bridge.task(1,project_root=root/'rail',root=old,kind='video')
            self.assertEqual(value['outcome'],'complete')
            self.assertEqual(value['candidate_items'][0],{'id':11,'title':'Train','kind':'video','state':'hold',
                'asset_id':digest,'source_url':'https://example.com/train','downloaded':True,'approved':True})
            self.assertEqual(value['candidate_items'][1]['kind'],'web')
            self.assertFalse(value['candidate_items'][1]['downloaded']);self.assertFalse(value['candidate_items'][1]['approved'])
            self.assertEqual(db.read_bytes(),before)
            with sqlite3.connect(db) as c:c.execute("UPDATE asset SET status='processing'")
            item=bridge.task(1,project_root=root/'rail',root=old)['candidate_items'][0]
            self.assertFalse(item['downloaded']);self.assertFalse(item['approved'])

    def test_approval_requires_authorization_and_publish_permission(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.job_fixture(root)
            for change in ('unauthorized','rejected','publish'):
                with sqlite3.connect(db) as c:
                    segment=json.loads(c.execute('SELECT payload FROM segment').fetchone()[0])
                    segment['authorizationVerified']=change!='unauthorized'
                    c.execute('UPDATE segment SET payload=?,state=?',(json.dumps(segment),'rejected' if change=='rejected' else 'approved'))
                    if change=='publish':c.execute('UPDATE commons_job SET payload=?',(json.dumps({'purpose':'publish'}),))
                item=bridge.task(1,project_root=root/'rail',root=old)['candidate_items'][0]
                self.assertTrue(item['downloaded']);self.assertFalse(item['approved'])

    def test_http_candidate_does_not_invent_download_or_approval(self):
        with patch.object(bridge,'_request',return_value={'jobs':[{'id':1,'state':'done','result':{}}],
                'candidates':[{'page_id':11,'job_id':1,'asset':'a'*64,'payload':{'mime':'video/mp4'}}]}):
            item=bridge.task(1)['candidate_items'][0]
        self.assertFalse(item['downloaded']);self.assertFalse(item['approved'])

    def test_task_classification_and_existing_cooldown_are_read_only(self):
        cases=[('HTTP Error 429: Too Many Requests','rate_limited'),('HTTP Error 503: unavailable','rate_limited'),
               ('<urlopen error EOF occurred in violation of protocol>','network')]
        for error,outcome in cases:
            with self.subTest(error=error),patch.object(bridge,'_request',return_value={'jobs':[{'id':1,'state':'failed',
                    'result':{'errors':[error],'retry_after_seconds':73,'cooldown_until':1900000000}}],'candidates':[]}):
                value=bridge.task(1)
                self.assertEqual(value['outcome'],outcome)
                self.assertEqual(value['retry_after_seconds'],73);self.assertEqual(value['cooldown_until'],1900000000)
        with patch.object(bridge,'_request',return_value={'jobs':[{'id':1,'state':'done','result':{}}],'candidates':[]}):
            self.assertEqual(bridge.task(1)['outcome'],'empty')
        with patch.object(bridge,'_request',return_value={'jobs':[{'id':1,'state':'running','result':{}}],'candidates':[]}):
            self.assertIsNone(bridge.task(1)['outcome'])

    def test_iso_cooldown_and_no_candidate_metadata_fabrication(self):
        with patch.object(bridge.time,'time',return_value=0):
            value=bridge._task_outcome({'state':'failed','result':{'errors':['HTTP 503'],'cooldown_until':'1970-01-01T00:02:00Z'}},[])
        self.assertEqual(value['retry_after_seconds'],120);self.assertEqual(value['cooldown_until'],120)
        item=bridge.candidate_summary({'page_id':3,'payload':{'discovery':'https://example.com/?api_key=hidden'}})
        self.assertEqual(item['source_url'],'https://example.com/')
        self.assertEqual(item['kind'],'web');self.assertFalse(item['downloaded'])

    def test_find_request_recovers_exact_job_without_writing_or_posting(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.job_fixture(root)
            before=db.read_bytes()
            with patch.object(bridge,'_request') as request:
                found=bridge.find_request(root/'rail','a'*64,root=old)
                self.assertEqual(found['id'],1)
                self.assertIsNone(bridge.find_request(root/'rail','b'*64,root=old))
                request.assert_not_called()
            self.assertEqual(db.read_bytes(),before)
            with self.assertRaises(bridge.LegacyError):bridge.find_request(root/'rail','invalid',root=old)

    def test_inventory_enforces_approval_type_and_purpose_read_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.fixture(root)
            before=db.read_bytes()
            found=bridge.approved_videos(root/'rail-content-studio',root=old)
            self.assertEqual(len(found),1);self.assertEqual(found[0]['start'],.5)
            self.assertEqual(bridge.approved_videos(root/'rail-content-studio',root=old,purpose='publish'),[])
            self.assertEqual(db.read_bytes(),before)
            with sqlite3.connect(db) as c:c.execute("UPDATE segment SET state='hold'")
            self.assertEqual(bridge.approved_videos(root/'rail-content-studio',root=old),[])
            with sqlite3.connect(db) as c:
                c.execute("UPDATE segment SET state='approved'");c.execute("UPDATE asset SET extension='.jpg'")
            self.assertEqual(bridge.approved_videos(root/'rail-content-studio',root=old),[])

    def test_export_rejects_changed_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.fixture(root)
            (old/'data/media'/digest/'original.mp4').write_bytes(b'changed')
            with self.assertRaisesRegex(bridge.LegacyError,'已变化'):
                bridge.export_approved_videos(root/'rail-content-studio',root=old)

    def real_video_fixture(self, root, extension='.webm', *, preview=False, audio_only=False):
        try:exe=ref.tool('ffmpeg');ref.tool('ffprobe')
        except Exception as exc:self.skipTest(str(exc))
        video=root/('fixture'+extension)
        if audio_only:
            command=[exe,'-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=2','-c:a','aac',str(video)]
        else:
            codec={'.webm':'libvpx-vp9','.ogv':'libtheora','.mp4':'libx264'}[extension]
            command=[exe,'-v','error','-y','-f','lavfi','-i','testsrc2=size=96x64:rate=10:duration=2',
                     '-an','-c:v',codec,str(video)]
        ref.run(command)
        old,db,digest=self.fixture(root,video.read_bytes())
        folder=old/'data/media'/digest
        if preview:
            (folder/'original.mp4').rename(folder/('source'+extension))
            ref.run([exe,'-v','error','-y','-i',str(video),'-an','-c:v','libx264',str(folder/'original.mp4')])
        elif extension!='.mp4':
            (folder/'original.mp4').rename(folder/('original'+extension))
            with sqlite3.connect(db) as c:c.execute('UPDATE asset SET extension=?',(extension,))
        return old,db,digest

    def test_downloaded_webm_is_read_only_and_does_not_grant_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.real_video_fixture(root)
            with sqlite3.connect(db) as c:
                c.execute("UPDATE segment SET state='hold'")
                c.executescript('CREATE TABLE commons_candidate(page_id INTEGER,payload TEXT,asset TEXT);'
                               'CREATE TABLE source(asset TEXT,name TEXT,uploader TEXT,origin TEXT);')
                c.execute('INSERT INTO commons_candidate VALUES(?,?,?)',(12,json.dumps({
                    'title':'Train footage','mime':'video/webm','provider':'Wikimedia Commons',
                    'discovery':'https://commons.wikimedia.org/wiki/File:Fixture.webm',
                    'mediaUrl':'https://upload.wikimedia.org/fixture.webm','license':'CC BY 4.0','author':'Fixture author'}),digest))
                c.execute('INSERT INTO source VALUES(?,?,?,?)',(digest,'Fixture','Fixture importer','https://example.com/source'))
            before=db.read_bytes();files={str(p):p.read_bytes() for p in old.rglob('*') if p.is_file()}
            records=bridge.downloaded_videos(root/'rail',asset_ids=[digest],root=old)
            self.assertEqual(len(records),1);record=records[0]
            self.assertEqual(record['asset_id'],digest);self.assertEqual(record['file_sha256'],digest)
            self.assertEqual(record['content_hash'],digest);self.assertEqual(record['extension'],'.webm')
            self.assertEqual(Path(record['path']).name,'original.webm')
            self.assertAlmostEqual(record['duration'],2,delta=.1)
            self.assertEqual(record['metadata']['codec'],'vp9')
            self.assertEqual(record['metadata']['width'],96);self.assertEqual(len(record['source_evidence']),2)
            self.assertNotIn('approval',record);self.assertNotIn('approved',record)
            self.assertEqual(bridge.approved_videos(root/'rail',root=old),[])
            self.assertEqual(db.read_bytes(),before)
            self.assertEqual({str(p):p.read_bytes() for p in old.rglob('*') if p.is_file()},files)
            self.assertFalse((root/'rail').exists())

    def test_legacy_webm_preview_uses_original_hash_for_screening_and_export(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.real_video_fixture(root,preview=True)
            folder=old/'data/media'/digest
            self.assertNotEqual(hashlib.sha256((folder/'original.mp4').read_bytes()).hexdigest(),digest)
            candidate=bridge.downloaded_videos(root/'rail',[digest],root=old)[0]
            self.assertEqual(Path(candidate['path']).name,'source.webm')
            self.assertEqual(candidate['file_sha256'],digest)
            before=db.read_bytes()
            result=bridge.export_approved_videos(root/'rail',root=old,asset_ids={digest})
            self.assertEqual(len(result),1);record=result[0]
            path=root/'rail'/record['file']
            self.assertEqual(path.suffix,'.mp4');self.assertEqual(record['source_interval'],[.5,1.5])
            self.assertEqual(bridge._probe_video_source(path)['codec'],'h264')
            self.assertAlmostEqual(record['clip_s'],1,delta=.11)
            self.assertEqual(record['legacy_asset_id'],digest)
            self.assertEqual(db.read_bytes(),before)
            self.assertEqual(hashlib.sha256((folder/'source.webm').read_bytes()).hexdigest(),digest)

    def test_full_approved_webm_and_ogv_export_to_mp4(self):
        for extension in ('.webm','.ogv'):
            with self.subTest(extension=extension),tempfile.TemporaryDirectory() as temp:
                root=Path(temp);old,db,digest=self.real_video_fixture(root,extension)
                with sqlite3.connect(db) as c:
                    payload=json.loads(c.execute('SELECT payload FROM segment').fetchone()[0])
                    payload.update(start=0,end=2)
                    c.execute('UPDATE segment SET payload=?',(json.dumps(payload),))
                self.assertEqual(bridge.downloaded_videos(root/'rail',[digest],root=old)[0]['extension'],extension)
                self.assertEqual(len(bridge.approved_videos(root/'rail',root=old)),1)
                self.assertEqual(bridge.export_approved_videos(root/'rail',root=old,purpose='publish'),[])
                result=bridge.export_approved_videos(root/'rail',root=old)
                self.assertEqual(len(result),1)
                exported=root/'rail'/result[0]['file']
                self.assertEqual(exported.suffix,'.mp4')
                self.assertEqual(bridge._probe_video_source(exported)['codec'],'h264')
                self.assertAlmostEqual(result[0]['clip_s'],2,delta=.11)
                with sqlite3.connect(db) as c:c.execute("UPDATE segment SET state='rejected'")
                self.assertEqual(bridge.export_approved_videos(root/'rail',root=old),[])

    def test_webm_preview_tail_rounding_never_extends_source_or_approval(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.real_video_fixture(root,preview=True)
            with sqlite3.connect(db) as c:
                payload=json.loads(c.execute('SELECT payload FROM segment').fetchone()[0])
                payload.update(start=0,end=2.01)
                c.execute('UPDATE segment SET payload=?',(json.dumps(payload),))
                c.execute('UPDATE asset SET info=?',(json.dumps({'duration':2.01}),))
            approved=bridge.approved_videos(root/'rail',root=old)[0]
            self.assertLessEqual(approved['end'],2)
            exported=bridge.export_approved_videos(root/'rail',root=old)[0]
            self.assertEqual(exported['source_interval'],[0,2])
            self.assertAlmostEqual(exported['clip_s'],2,delta=.11)

    def test_downloaded_excludes_changed_bytes_unsafe_paths_and_nonready_assets(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.real_video_fixture(root)
            original=old/'data/media'/digest/'original.webm';data=original.read_bytes()
            original.write_bytes(b'changed')
            self.assertEqual(bridge.downloaded_videos(root/'rail',[digest],root=old),[])
            original.unlink();outside=root/'outside.webm';outside.write_bytes(data);original.symlink_to(outside)
            self.assertEqual(bridge.downloaded_videos(root/'rail',[digest],root=old),[])
            original.unlink();original.write_bytes(data)
            with sqlite3.connect(db) as c:c.execute("UPDATE asset SET status='processing'")
            self.assertEqual(bridge.downloaded_videos(root/'rail',[digest],root=old),[])
            with self.assertRaises(bridge.LegacyError):bridge.downloaded_videos(root/'rail',['../outside'],root=old)

    def test_downloaded_rejects_audio_only_and_invalid_container(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.real_video_fixture(root,'.mp4',audio_only=True)
            self.assertEqual(bridge.downloaded_videos(root/'rail',[digest],root=old),[])
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);old,db,digest=self.fixture(root,b'not a movie')
            self.assertEqual(bridge.downloaded_videos(root/'rail',[digest],root=old),[])

    def test_actual_export_trims_to_reviewed_interval(self):
        try:exe=ref.tool('ffmpeg');ref.tool('ffprobe')
        except Exception as exc:self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);video=root/'fixture.mp4'
            ref.run([exe,'-v','error','-y','-f','lavfi','-i','color=blue:size=320x240:rate=30:duration=2',
                     '-c:v','libx264',str(video)])
            old,db,digest=self.fixture(root,video.read_bytes());project=root/'rail-content-studio'
            rows=bridge.export_approved_videos(project,root=old)
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['source_interval'],[.5,1.5])
            self.assertAlmostEqual(rows[0]['clip_s'],1,delta=1/30+.01)
            self.assertTrue((project/rows[0]['file']).is_file())
            self.assertEqual(bridge.export_approved_videos(project,root=old)[0]['id'],rows[0]['id'])
            with sqlite3.connect(db) as c:c.execute("UPDATE segment SET state='rejected'")
            self.assertEqual(bridge.export_approved_videos(project,root=old),[])


if __name__=='__main__':unittest.main()
