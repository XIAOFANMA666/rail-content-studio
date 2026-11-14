"""Contract and local render checks; model responses are explicitly fixtures."""
import copy
import io
import json
from pathlib import Path
import tempfile
import threading
import shutil
import time
import unittest
from unittest.mock import patch

import media_library as media
import reference_video as ref


def fixture_analysis():
    return {'shots':[{'id':'s01','start_frame':0,'end_frame':30,'subject':'测试色块',
        'action':'静止','shot_size':'全景','scene':'合成测试','direction':'无','camera':'固定',
        'composition':'居中','tags':['fixture'],'queries':['fixture test'],'transition':'cut'}],
        'text_events':[{'text':'TEST','start_frame':0,'end_frame':30,'font_name':'Arial',
                        'x':.5,'y':.8,'size':.1,'color':'#FFFFFF'}]}


class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.service=ref.Service(self.root,self.root/'output')
        self.technical={'total_frames':30,'fps':30,'width':320,'height':240,'duration':1,'audio':True}

    def upload(self):
        with patch.object(self.service,'start'):
            return self.service.upload(io.BytesIO(b'fixture'),7,'example.mp4')

    def test_timeline_must_cover_reference_without_overlap(self):
        for value in (-1,1,True,0.0):
            data=fixture_analysis();data['shots'][0]['start_frame']=value
            with self.subTest(value=value),self.assertRaises(media.MediaError):
                ref.normalize_analysis(data,self.technical)
        data=fixture_analysis();data['shots'][0]['end_frame']=29
        with self.assertRaises(media.MediaError):ref.normalize_analysis(data,self.technical)
        data=fixture_analysis();data['shots'][0]['id']='../../outside'
        with self.assertRaises(media.MediaError):ref.normalize_analysis(data,self.technical)

    def test_malformed_geometry_and_caption_ranges_rejected(self):
        for key,value in [('x',float('nan')),('y',2),('color','red'),('start_frame',-1),('end_frame',31)]:
            data=fixture_analysis();data['text_events'][0][key]=value
            with self.subTest(key=key),self.assertRaises(media.MediaError):
                ref.normalize_analysis(data,self.technical)

    def test_model_cannot_claim_verified_font(self):
        data=fixture_analysis();data['text_events'][0]['font_status']='verified'
        parsed=ref.normalize_analysis(data,self.technical)
        self.assertEqual(parsed['text_events'][0]['font_status'],'inferred')

    def test_subtitle_coordinates_and_injection(self):
        data=fixture_analysis();data['text_events'][0]['text']=r'{\pos(1,2)}'
        ass=ref.make_ass(ref.normalize_analysis(data,self.technical),self.technical)
        self.assertIn(r'\pos(160.00,192.00)',ass)
        self.assertNotIn(r'{\pos(1,2)}',ass)

    def test_upload_is_isolated_and_truncation_removed(self):
        job=self.upload()
        self.assertEqual((self.service.directory(job['id'])/'reference.mp4').read_bytes(),b'fixture')
        self.assertFalse((self.root/'output/result.json').exists())
        with self.assertRaises(media.MediaError):
            self.service.upload(io.BytesIO(b'bad'),10,'broken.mp4')
        self.assertEqual(len(list(self.service.base.iterdir())),1)

    def test_upload_limits_and_extensions(self):
        for length,name in [(0,'a.mp4'),(ref.MAX_BYTES+1,'a.mp4'),(1,'a.html')]:
            with self.subTest(name=name,length=length),self.assertRaises(media.MediaError):
                self.service.upload(io.BytesIO(b'x'),length,name)

    def test_paths_and_concurrent_jobs(self):
        for jid in ('../secret','x'*32):
            with self.assertRaises(media.MediaError):self.service.get(jid)
        self.service.busy.add('fixture')
        with self.assertRaises(media.MediaError):self.service.upload(io.BytesIO(b'x'),1,'a.mp4')

    def test_restart_pauses_without_replaying(self):
        job=self.upload();self.service.save(job,status='matching')
        restarted=ref.Service(self.root,self.root/'output')
        self.assertEqual(restarted.get(job['id'])['status'],'interrupted')
        self.assertFalse(restarted.busy)

    def test_restart_restores_only_enabled_material_search_watchers(self):
        enabled=self.upload()
        enabled['material_search']={'enabled':True,'shots':{'s01':{'task_records':[{'task_id':17}]}}}
        self.service.save(enabled,status='awaiting_materials')
        disabled=self.upload()
        disabled['material_search']={'enabled':False}
        self.service.save(disabled,status='awaiting_materials')
        with patch.object(ref.Service,'start_watcher') as watcher, patch.object(ref.Service,'start') as worker:
            restarted=ref.Service(self.root,self.root/'output')
        watcher.assert_called_once_with(enabled['id'])
        worker.assert_not_called()
        self.assertEqual(restarted.get(enabled['id'])['material_search']['shots']['s01']['task_records'],[{'task_id':17}])
        self.assertEqual(restarted.get(disabled['id'])['status'],'awaiting_materials')
        self.assertFalse(restarted.busy)

    def test_stale_watcher_does_not_remove_replacement_token(self):
        job=self.upload();jid=job['id']
        self.service.save(job,status='awaiting_materials')
        self.service.watchers[jid]='replacement-token'
        with patch.object(ref.time,'sleep'), patch.object(self.service,'search') as search:
            self.service.watch_materials(jid,'expired-token')
        self.assertEqual(self.service.watchers[jid],'replacement-token')
        search.assert_not_called()

    def test_old_watcher_finishing_after_restart_preserves_new_watcher(self):
        job=self.upload();jid=job['id']
        job.update(gaps=[{'shot_id':'s01'}],legacy_search_tasks={'s01':{'task_id':19}})
        self.service.save(job,status='awaiting_materials')
        self.service.watchers[jid]='original-token'
        def start_new_worker(actual_jid):
            self.assertEqual(actual_jid,jid)
            self.assertNotIn(jid,self.service.watchers)
            # A fast worker can need a new watcher before this one leaves finally.
            self.service.watchers[jid]='new-worker-token'
        with patch.object(ref.time,'sleep'), \
             patch.object(ref.legacy,'config',return_value={'available':False}), \
             patch.object(ref.legacy,'export_approved_videos',return_value=[{'id':'approved_old_clip','legacy_asset_id':'old_asset'}]), \
             patch.object(ref.material_search,'related_asset_ids',return_value={'old_asset'}) as related, \
             patch.object(self.service,'start',side_effect=start_new_worker) as worker:
            self.service.watch_materials(jid,'original-token')
        worker.assert_called_once_with(jid)
        related.assert_called_once()
        self.assertEqual(self.service.watchers[jid],'new-worker-token')

    def test_watcher_waits_for_connection_retry_deadline(self):
        job=self.upload();jid=job['id']
        job['material_search']={'enabled':True,'state':'connection_error','next_retry_at':120}
        self.service.save(job,status='awaiting_materials')
        self.service.watchers[jid]='watch-token'
        def complete_search(current):
            self.service.save(current,status='cancelled')
        with patch.object(ref.time,'sleep'), patch.object(ref.time,'time',side_effect=[110,120]), \
             patch.object(ref.legacy,'config',return_value={'available':True}), \
             patch.object(self.service,'search',side_effect=complete_search) as search:
            self.service.watch_materials(jid,'watch-token')
        search.assert_called_once()
        self.assertNotIn(jid,self.service.watchers)

    def test_cancel_waiting_material_search_disables_restart_recovery(self):
        job=self.upload();jid=job['id']
        job['material_search']={'enabled':True,'shots':{'s01':{'task_records':[{'task_id':17}]}}}
        self.service.save(job,status='awaiting_materials')
        self.service.watchers[jid]='watch-token'
        self.service.cancel(jid)
        saved=self.service.get(jid)
        self.assertEqual(saved['status'],'cancelled')
        self.assertFalse(saved['material_search']['enabled'])
        self.assertEqual(saved['material_search']['shots']['s01']['task_records'],[{'task_id':17}])
        with patch.object(ref.Service,'start_watcher') as watcher:
            ref.Service(self.root,self.root/'output')
        watcher.assert_not_called()

    def test_missing_search_key_keeps_gap(self):
        job=self.upload();job['gaps']=[{'shot_id':'s01'}]
        with patch.object(ref.llm,'load_env',return_value={}):self.service.search(job)
        self.assertEqual(self.service.get(job['id'])['status'],'awaiting_materials')

    def test_content_model_once_never_replays_failed_request(self):
        job=self.upload()
        with patch.object(media,'model_json',side_effect=media.MediaError('fixture timeout')) as model:
            with self.assertRaises(media.MediaError):
                self.service.model_once(job,'fixture',[],100)
        model.assert_called_once()

    def test_cancel_stops_next_step_without_losing_completed_data(self):
        job=self.upload();jid=job['id']
        self.service.busy.add(jid);self.service.cancellations[jid]=threading.Event()
        self.service.cancel(jid)
        with self.assertRaises(ref.Cancelled):self.service.check(job)
        self.service.save(job,status='matching',stage='late update')
        self.assertEqual(self.service.get(jid)['status'],'cancelling')
        self.assertTrue((self.service.directory(jid)/job['reference']).exists())

    def test_unsupported_effect_blocks_generation(self):
        job=self.upload();job['technical']=self.technical
        job['analysis']=ref.normalize_analysis(fixture_analysis(),self.technical)
        job['analysis']['shots'][0]['transition']='dissolve'
        self.service.render(job)
        self.assertEqual(self.service.get(job['id'])['status'],'needs_review')
        self.assertNotIn('output_url',self.service.get(job['id']))

    def test_unresolved_reference_element_blocks_finished_output(self):
        job=self.upload();job['technical']=self.technical
        data=fixture_analysis();data['unresolved']=['逐字字幕尚未完成']
        job['analysis']=ref.normalize_analysis(data,self.technical)
        self.service.render(job)
        result=self.service.get(job['id'])
        self.assertEqual(result['status'],'needs_review')
        self.assertIn('逐字字幕',result['error'])
        self.assertNotIn('output_url',result)

    def test_changed_storyboard_drops_stale_search_and_match(self):
        job=self.upload();job['technical']=self.technical
        job['analysis']=ref.normalize_analysis(fixture_analysis(),self.technical)
        job.update(matches={'s01':{'clip_id':'old'}},search_tasks={'s01':['old-task']},
                   legacy_search_tasks={'s01':{'task_id':1}})
        self.service.save(job)
        changed=fixture_analysis();changed['shots'][0]['subject']='火车'
        with patch.object(threading.Thread,'start'):
            self.service.start(job['id'],changed)
        result=self.service.get(job['id'])
        self.assertEqual(result['matches'],{})
        self.assertEqual(result['search_tasks'],{})
        self.assertEqual(result['legacy_search_tasks'],{})


class ReferenceRenderTests(unittest.TestCase):
    def test_upload_worker_to_video_with_explicit_model_fixtures(self):
        try:exe=ref.tool('ffmpeg');ref.tool('ffprobe')
        except media.MediaError as exc:self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);(root/'data').mkdir()
            shutil.copytree(Path(ref.__file__).parent/ref.SKILL.parent,root/ref.SKILL.parent)
            source=root/'source.mp4';clip=root/'clip.mp4'
            ref.run([exe,'-v','error','-y','-f','lavfi','-i','color=red:size=320x240:rate=30:duration=1',
                     '-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-c:a','aac','-shortest',str(source)])
            ref.run([exe,'-v','error','-y','-f','lavfi','-i','color=blue:size=320x240:rate=30:duration=2',
                     '-c:v','libx264',str(clip)])
            (root/'data/library.json').write_text(json.dumps({'clips':[{'id':'m01','file':'clip.mp4','tags':['fixture']}]}))
            service=ref.Service(root,root/'output')
            data=fixture_analysis();shot={**data['shots'][0],'text_events':data['text_events']}
            checks={k:True for k in ('subject','action','shot_size','scene','direction','camera','composition')}
            outputs=[(shot,{'provider':'TEST FIXTURE'}),({'match':True,'src_in':.2,'checks':checks,'reason':'fixture'},{}),
                     ({'match':True,'reason':'Synthetic response, not real visual evaluation'}, {})]
            with patch.object(media,'model_json',side_effect=outputs) as model:
                job=service.upload(io.BytesIO(source.read_bytes()),source.stat().st_size,'test.mp4')
                deadline=time.monotonic()+30
                while job['id'] in service.busy and time.monotonic()<deadline:time.sleep(.05)
                actual=service.get(job['id'])
                self.assertEqual(actual['status'],'preview_ready',actual.get('error'))
                self.assertEqual(model.call_count,3)
                directory=service.directory(job['id'])
                for name in ('preview.mp4','reference-spec.json','match-plan.json','library.json','acceptance.json'):
                    self.assertTrue((directory/name).is_file(),name)
                self.assertTrue(actual.get('skill_sha256'))

    def test_actual_render_keeps_dimensions_duration_audio_and_new_footage(self):
        try:exe=ref.tool('ffmpeg');ref.tool('ffprobe')
        except media.MediaError as exc:self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);service=ref.Service(root,root/'output')
            source=root/'reference.mp4';clip=root/'clip.mp4'
            ref.run([exe,'-v','error','-y','-f','lavfi','-i','color=red:size=320x240:rate=30:duration=1',
                     '-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-c:a','aac','-shortest',str(source)])
            ref.run([exe,'-v','error','-y','-f','lavfi','-i','color=blue:size=320x240:rate=30:duration=2',
                     '-c:v','libx264',str(clip)])
            with patch.object(service,'start'):
                job=service.upload(io.BytesIO(source.read_bytes()),source.stat().st_size,'reference.mp4')
            job.update(technical=ref.probe(source),analysis=ref.normalize_analysis(fixture_analysis(),ref.probe(source)))
            job['matches']={'s01':{'clip_id':'fixture','file':'clip.mp4','src_in':.5,'duration':1}}
            service.render(job)
            actual=service.get(job['id']);self.assertEqual(actual['status'],'preview_ready')
            output=service.directory(job['id'])/'preview.mp4';tech=ref.probe(output)
            self.assertEqual((tech['width'],tech['height']),(320,240));self.assertTrue(tech['audio'])
            self.assertAlmostEqual(tech['duration'],1,delta=1/30+.02)
            pixels=ref.run([exe,'-v','error','-ss','0.2','-i',str(output),'-frames:v','1','-vf','scale=1:1',
                            '-f','rawvideo','-pix_fmt','rgb24','-']).stdout
            self.assertGreater(pixels[2],pixels[0]+100)
            self.assertTrue((service.directory(job['id'])/'acceptance.json').is_file())
            self.assertFalse((root/'output/result.json').exists())


if __name__=='__main__':unittest.main()
