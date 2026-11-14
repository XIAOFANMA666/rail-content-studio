"""Local-only HTTP interface for reference-driven generation."""
import json
import re
from urllib.parse import parse_qs, unquote, urlparse

import media_http
import media_library as media
import reference_video as reference
import reference_settings
import reference_legacy_media as legacy


def handle_get(handler,root,out):
    parsed=urlparse(handler.path)
    if not (parsed.path.startswith('/api/reference/') or parsed.path.startswith('/reference-files/')):
        return False
    try:
        service=reference.service(root,out)
        if parsed.path=='/api/reference/config':
            handler._send(200,service.config())
        elif parsed.path=='/api/reference/jobs':
            handler._send(200,{'items':service.listing()})
        elif parsed.path=='/api/reference/job':
            jid=parse_qs(parsed.query).get('id',[''])[0]
            job=service.get(jid)
            job['busy']=jid in service.busy
            job['legacy_jobs']=[]
            for sid,record in job.get('legacy_search_tasks',{}).items():
                try:
                    state=legacy.task(record['task_id'],project_root=root)
                    candidates=reference.material_search.candidate_items(job,sid,root)
                    progress=job.get('material_search',{}).get('shots',{}).get(sid,{})
                    result=state.get('result',{})
                    for candidate in candidates:
                        review=job.get('material_search',{}).get('content_reviews',{}).get(sid,{}).get(candidate.get('asset_id'),{})
                        if review:
                            candidate.update(content_review=review.get('status'),content_reason=review.get('reason',''),
                                content_phase=review.get('phase'),content_rejection_scope=review.get('rejection_scope'))
                    job['legacy_jobs'].append({'shot_id':sid,'id':record['task_id'],'state':state.get('state'),
                        'candidates':len(candidates),'candidate_items':candidates,
                        'reason':progress.get('reason') or state.get('result',{}).get('stopReason',''),
                        'errors':state.get('result',{}).get('errors',[]),
                        **({'search_rounds':sum(isinstance(r,dict) and r.get('action')=='search' and r.get('complete',True)
                              for r in (result.get('rounds') or [])),
                            'agent_planning_calls':sum(isinstance(r,dict) and r.get('called') is True
                              for r in (result.get('planning') or []))} if isinstance(result.get('rounds'),list) else {}),
                        **{key:progress.get(key) for key in ('attempts','plan_attempts','next_retry_at','retry_reason','request_fingerprint')}})
                except legacy.LegacyError:
                    job['legacy_jobs'].append({'shot_id':sid,'state':'读取失败'})
            handler._send(200,job)
        else:
            match=re.fullmatch(r'/reference-files/([a-f0-9]{32})/(reference\.(?:mp4|mov|m4v)|preview\.mp4|reference-spec\.json|match-plan\.json|material-search-plan\.json|acceptance\.json|captions\.ass)',parsed.path)
            if not match:
                raise media.MediaError('文件不存在')
            directory=service.directory(match[1])
            file=directory/match[2]
            if not file.is_file():
                handler._send(404,{'error':'文件尚未生成'})
            else:
                media_http.stream_file(handler,file)
    except (media.MediaError,ValueError,TypeError):
        handler._send(400,{'error':'参考任务或文件无效'})
    except Exception:
        handler._send(500,{'error':'参考任务读取失败'})
    return True


def handle_post(handler,root,out,port):
    path=urlparse(handler.path).path
    if not path.startswith('/api/reference/'):
        return False
    if (handler.headers.get('Origin') not in (f'http://localhost:{port}',f'http://127.0.0.1:{port}') or
        handler.headers.get('Host') not in (f'localhost:{port}',f'127.0.0.1:{port}')):
        handler._send(403,{'error':'请从本地工作台导入参考视频'})
        return True
    try:
        length=int(handler.headers.get('Content-Length',0))
        ctype=handler.headers.get('Content-Type','').split(';')[0]
        service=reference.service(root,out)
        if path=='/api/reference/import':
            if ctype not in ('video/mp4','video/quicktime','video/x-m4v','application/octet-stream'):
                raise media.MediaError('上传类型应为视频文件')
            name=unquote(handler.headers.get('X-File-Name','reference.mp4'))
            handler.connection.settimeout(120)
            job=service.upload(handler.rfile,length,name)
            handler._send(202,job)
        elif path=='/api/reference/settings':
            if ctype!='application/json' or not 0<length<=16384:
                raise media.MediaError('配置请求格式或大小无效')
            try:
                state=reference_settings.save_settings(root,json.loads(handler.rfile.read(length)))
            except ValueError as exc:
                raise media.MediaError(str(exc)) from None
            handler._send(200,state)
        elif path in ('/api/reference/resume','/api/reference/cancel'):
            if ctype!='application/json' or not 0<length<=256*1024:
                raise media.MediaError('任务请求格式或大小无效')
            body=json.loads(handler.rfile.read(length))
            if path.endswith('/cancel'):
                service.cancel(body.get('id'))
            else:
                service.start(body.get('id'),body.get('analysis'),search_only=body.get('search_only') is True)
            handler._send(202,service.get(body.get('id')))
        else:
            handler._send(404,{'error':'接口不存在'})
    except (media.MediaError,ValueError,TypeError,AttributeError) as exc:
        handler._send(400,{'error':str(exc) if isinstance(exc,media.MediaError) else '参考视频请求无效'})
    except Exception:
        handler._send(500,{'error':'任务提交失败，已完成结果保留'})
    return True
