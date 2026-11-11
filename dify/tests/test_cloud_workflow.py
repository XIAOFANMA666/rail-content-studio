import importlib.util
import json
from pathlib import Path
import unittest


ROOT=Path(__file__).resolve().parents[1]


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/(name+'.py'))
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


asr=load('cloud_asr_code')
builder=load('build_cloud_workflow')


class CloudWorkflowTest(unittest.TestCase):
    def test_request_preparation_needs_no_key_and_rejects_unapproved_endpoint(self):
        video='https://upload.dify.ai/files/example.mp4?signature=test'
        for base in ('https://example.com/api/v1',
                     'http://dashscope.aliyuncs.com/api/v1',
                     'https://dashscope.aliyuncs.com.evil.test/api/v1',
                     'https://user:password@dashscope.aliyuncs.com/api/v1'):
            result=asr.prepare_request(video,'en-US',base)
            self.assertEqual(result['ready'],0)
            self.assertEqual(result['submit_url'],'')
        result=asr.prepare_request(video,'en-US','https://dashscope.aliyuncs.com/api/v1')
        self.assertEqual(result['ready'],1)
        self.assertNotIn('test-key',json.dumps(result))
        self.assertEqual(json.loads(result['request_body'])['input']['file_urls'],[video])
        self.assertEqual(json.loads(result['request_body'])['parameters']['language_hints'],['en'])

    def test_submission_requires_success_and_valid_task_id(self):
        base='https://dashscope.aliyuncs.com/api/v1'
        job={'output':{'task_id':'12345678-abcd-1234','task_status':'PENDING'}}
        result=asr.parse_submission(json.dumps(job),200,base)
        self.assertEqual(result['poll_url'],base+'/tasks/12345678-abcd-1234')
        for code,body in [(401,json.dumps(job)),(200,'{}'),(200,'not JSON')]:
            self.assertEqual(asr.parse_submission(body,code,base)['ready'],0)
        job['output']['task_id']='../../credentials'
        self.assertEqual(asr.parse_submission(json.dumps(job),200,base)['ready'],0)

    def test_poll_only_accepts_successful_official_transcript_url(self):
        pending=asr.parse_poll(json.dumps({'output':{'task_status':'RUNNING'}}),200)
        self.assertEqual(pending['done'],0)
        self.assertEqual(pending['result_url'],'')
        def payload(url,status='SUCCEEDED'):
            return json.dumps({'output':{'task_status':'SUCCEEDED','results':[{'subtask_status':status,'transcription_url':url}]}})
        for url in ('http://example.aliyuncs.com/result.json','https://example.org/result.json',
                    'https://example.aliyuncs.com.evil.test/result.json','https://user:pass@example.aliyuncs.com/result.json'):
            self.assertEqual(asr.parse_poll(payload(url),200)['result_url'],'')
        url='https://dashscope-result.oss-cn-beijing.aliyuncs.com/result.json?signature=test'
        self.assertEqual(asr.parse_poll(payload(url),200)['result_url'],url)
        self.assertEqual(asr.parse_poll(payload(url,'FAILED'),200)['result_url'],'')
        self.assertEqual(asr.parse_poll(payload(url),503)['done'],1)

    def test_audio_ok_requires_valid_nonempty_result_after_http_success(self):
        body=json.dumps({'transcripts':[{'text':'Take the train to Scotland.'}]})
        audio=asr.unpack_audio(asr.parse_result(body,200)['audio'])
        self.assertEqual(audio,{'audio_status':'ok','transcript':'Take the train to Scotland.'})
        for content,code in [(body,500),('{}',200),('bad JSON',200),
                             (json.dumps({'transcripts':[{'text':''}]}),200),
                             (json.dumps({'transcripts':[{'text':'not a valid audio file'}]}),200)]:
            output=asr.unpack_audio(asr.parse_result(content,code)['audio'])
            self.assertEqual(output,{'audio_status':'unavailable','transcript':''})

    def test_attachment_json_is_single_and_body_still_works(self):
        body=json.dumps({'transcripts':[{'text':'Take the train to Scotland.'}]})
        expected={'audio_status':'ok','transcript':'Take the train to Scotland.'}
        self.assertEqual(asr.unpack_audio(asr.parse_result('',200,[body])['audio']),expected)
        self.assertEqual(asr.unpack_audio(asr.parse_result(body,200,[])['audio']),expected)
        for attachments in (None, [], [body,body], [None], [''], ['not JSON'], body):
            output=asr.unpack_audio(asr.parse_result('',200,attachments)['audio'])
            self.assertEqual(output,{'audio_status':'unavailable','transcript':''})
        self.assertEqual(asr.unpack_audio(asr.parse_result('',500,[body])['audio'])['audio_status'],'unavailable')
        self.assertEqual(asr.unpack_audio(asr.parse_result('not JSON',200,[body])['audio'])['audio_status'],'unavailable')

    def test_graph_guards_every_conditional_http_and_has_no_local_server(self):
        dsl=builder.build()
        nodes={node['id']:node for node in dsl['workflow']['graph']['nodes']}
        edges=dsl['workflow']['graph']['edges']
        self.assertEqual(next(v for v in dsl['workflow']['environment_variables'] if v['name']=='DASHSCOPE_API_KEY')['value'],'')
        self.assertNotIn('host.docker.internal',json.dumps(dsl['workflow']['graph']['edges']))
        for identifier in ('asr_submit','asr_result'):
            incoming=[edge for edge in edges if edge['target']==identifier]
            self.assertEqual(len(incoming),1)
            self.assertEqual(incoming[0]['sourceHandle'],'true')
            self.assertEqual(nodes[incoming[0]['source']]['data']['type'],'if-else')
        self.assertEqual(nodes['asr_poll']['parentId'],'asr_loop')
        self.assertEqual(nodes['asr_extract']['data']['type'],'document-extractor')
        self.assertEqual(nodes['asr_extract']['data']['variable_selector'],['asr_result','files'])
        self.assertTrue(nodes['asr_extract']['data']['is_array_file'])
        self.assertIn({'variable':'extracted_texts','value_selector':['asr_extract','text'],
                       'value_type':'array[string]'},nodes['asr_result_parse']['data']['variables'])
        self.assertEqual([edge['target'] for edge in edges if edge['source']=='asr_result'],['asr_extract'])
        self.assertEqual([edge['target'] for edge in edges if edge['source']=='asr_extract'],['asr_result_parse'])
        self.assertEqual({item['variable'] for item in nodes['asr_prepare']['data']['variables']},
                         {'video_url', 'language', 'base_url'})
        for node in nodes.values():
            if node['data']['type']=='code':
                self.assertFalse(any(variable['value_selector']==['env','DASHSCOPE_API_KEY']
                                     for variable in node['data']['variables']))
        self.assertEqual(nodes['asr_loop']['data']['loop_count'],30)
        self.assertEqual(nodes['vision']['data']['vision']['configs']['variable_selector'],['start','video'])
        self.assertEqual(nodes['report']['data']['code'],(ROOT/'cloud_report_code.py').read_text(encoding='utf-8'))
        self.assertEqual(nodes['end']['data']['outputs'],[{'value_selector':['report','report'],'value_type':'string','variable':'report'}])
        for node in nodes.values():
            if node['data']['type']=='code':
                compile(node['data']['code'],node['id'],'exec')
        for edge in edges:
            self.assertIn(edge['source'],nodes)
            self.assertIn(edge['target'],nodes)


if __name__=='__main__':
    unittest.main()
