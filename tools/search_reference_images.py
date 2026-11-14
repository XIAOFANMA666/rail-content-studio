import sys,time,json,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import reference_legacy_media as lib
root=Path(__file__).resolve().parent.parent
import argparse
parser=argparse.ArgumentParser(description='按已有参考分镜顺序搜图片候选')
parser.add_argument('job_id')
args=parser.parse_args()
if len(args.job_id)!=32 or any(c not in '0123456789abcdef' for c in args.job_id): parser.error('job_id须为32位十六进制')
d=root/'output/reference-jobs'/args.job_id
j=json.loads((d/'job.json').read_text()); dest=d/'image-search-results.json'
state=json.loads(dest.read_text()) if dest.exists() else {'purpose':'按分镜搜图片候选，不代替连续动作视频','shots':{}}
def save():
    p=dest.with_suffix('.tmp');p.write_text(json.dumps(state,ensure_ascii=False,indent=2));p.replace(dest)
for shot in j['analysis']['shots']:
    sid=shot['id']; row=state['shots'].setdefault(sid,{'subject':shot['subject'],'status':'pending'})
    request={'query':json.dumps(shot,ensure_ascii=False)[:990], 'kind':'image','keywords':shot['queries'][0], 'required':'图片候选：'+ '；'.join(str(shot.get(k,'')) for k in ('subject','shot_size','scene','composition')), 'duration':0,'place':'','mode':'fixed','purpose':'internal','reference_context':{'job_id':j['id'],'shot_id':sid,'attempt':1}}
    request['request_fingerprint']=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
    row['request']=request;save()
    deadline=time.time()+180
    while not row.get('task_id') and time.time()<deadline:
        try:
            old=lib.find_request(root,request['request_fingerprint']); r={'task_id':old['id']} if old else lib.search(request)
            row.update(r,status='submitted');save()
        except lib.LegacyBusy: time.sleep(3)
        except Exception as e: row.update(status='error',error=str(e));save();break
    if not row.get('task_id'):continue
    while time.time()<deadline:
        try:
            actual=lib.task(row['task_id'],project_root=root); row.update(status=actual['state'],candidates=actual.get('candidate_items',[]),result=actual.get('result',{}));save()
            if actual['state'] not in ('queued','running'):break
        except Exception as e: row['error']=str(e);save()
        time.sleep(2)
    print(sid,row.get('task_id'),row['status'],len(row.get('candidates',[])),flush=True)
state['batch_finished']=True;save()
