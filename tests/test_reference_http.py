"""Local HTTP tests; no model, search or paid request is made."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import reference_video as ref
import reference_http
import studio


class ReferenceHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.out=self.root/'output'
        self.server=studio.ThreadingHTTPServer(('127.0.0.1',0),studio.H)
        self.port=self.server.server_port
        self.patches=[patch.object(studio,'ROOT',str(self.root)),patch.object(studio,'OUT',str(self.out)),patch.object(studio,'PORT',self.port)]
        for p in self.patches:p.start();self.addCleanup(p.stop)
        self.service=ref.service(self.root,self.out)
        self.no_start=patch.object(self.service,'start');self.start_mock=self.no_start.start();self.addCleanup(self.no_start.stop)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()

    def request(self,path,body=None,origin=True,headers=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.port)
        h={'Content-Type':'video/mp4','X-File-Name':'example.mp4'}
        if origin:h['Origin']=f'http://127.0.0.1:{self.port}'
        h.update(headers or {})
        conn.request('POST' if body is not None else 'GET',path,body=body,headers=h)
        response=conn.getresponse();data=response.read();code=response.status;conn.close()
        return code,data

    def test_import_status_and_range_file(self):
        code,body=self.request('/api/reference/import',b'fixture-video')
        self.assertEqual(code,202);job=json.loads(body)
        self.start_mock.assert_called_once_with(job['id'])
        code,body=self.request('/api/reference/job?id='+job['id'])
        self.assertEqual(code,200);self.assertEqual(json.loads(body)['sha256'],job['sha256'])
        code,body=self.request(job['reference_url'],headers={'Range':'bytes=0-6'})
        self.assertEqual(code,206);self.assertEqual(body,b'fixture')

    def test_cross_origin_invalid_requests_and_traversal(self):
        code,_=self.request('/api/reference/import',b'x',origin=False);self.assertEqual(code,403)
        code,_=self.request('/api/reference/import',b'x',headers={'X-File-Name':'bad.html'});self.assertEqual(code,400)
        code,_=self.request('/reference-files/'+'a'*32+'/../../.env');self.assertEqual(code,400)
        self.assertFalse(list(self.service.base.glob('*/job.json')))

    def test_config_exposes_no_credentials(self):
        code,body=self.request('/api/reference/config');self.assertEqual(code,200)
        data=json.loads(body)
        self.assertTrue(all(type(v) is bool for v in data.values()))
        self.assertNotIn('api_key',data)

    def test_search_evidence_keeps_content_and_approval_separate(self):
        _,body=self.request('/api/reference/import',b'fixture-video')
        job=json.loads(body)
        job.update(legacy_search_tasks={'s01':{'task_id':54}},material_search={
            'shots':{'s01':{'plan_attempts':1}},
            'content_reviews':{'s01':{'asset-test':{'status':'deferred','phase':'complete',
                'rejection_scope':'sampled_frames_only','reason':'未检查部分仍需定位'}}}})
        self.service.save(job)
        snapshot={'state':'done','result':{
            'rounds':[{'action':'search','complete':True},{'action':'search','complete':False}],
            'planning':[{'called':True},{'called':False}]}}
        candidate={'id':'source-test','asset_id':'asset-test','kind':'video','downloaded':True,'approved':False}
        with patch.object(reference_http.legacy,'task',return_value=snapshot), \
             patch.object(ref.material_search,'candidate_items',return_value=[candidate]):
            code,body=self.request('/api/reference/job?id='+job['id'])
        self.assertEqual(code,200)
        row=json.loads(body)['legacy_jobs'][0]
        self.assertEqual((row['search_rounds'],row['agent_planning_calls']),(1,1))
        item=row['candidate_items'][0]
        self.assertEqual(item['content_review'],'deferred')
        self.assertEqual(item['content_rejection_scope'],'sampled_frames_only')
        self.assertFalse(item['approved'])

    def test_settings_are_private_and_not_echoed(self):
        body=json.dumps({'VIDEO_BASE_URL':'https://example.test/v1','VIDEO_MODEL':'vision-test',
                         'VIDEO_API_KEY':'synthetic-secret','PEXELS_API_KEY':'synthetic-source'}).encode()
        code,result=self.request('/api/reference/settings',body,headers={'Content-Type':'application/json'})
        self.assertEqual(code,200)
        self.assertNotIn(b'synthetic-secret',result)
        self.assertTrue(json.loads(result)['vision'])
        self.assertEqual((self.root/'.env').stat().st_mode & 0o777,0o600)


if __name__=='__main__':unittest.main()
