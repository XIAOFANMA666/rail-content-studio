"""Persistent, bounded execution of the embedded Skill's material-search plan.

The old library owns discovery/download/review. This coordinator owns which shot
requested each task, retries of that same task, and return to reference matching.
"""
import hashlib
import importlib.util
import json
import time
from pathlib import Path

import reference_legacy_media as legacy


SKILL_DIR = Path('skills/replicate-reference-video')
ACTIVE = {'queued', 'running'}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def semantic_plan_id(item):
    """Budget the complete shot contract, not policy edits or old keyword trials."""
    return digest({'schema':'complete-shot-requirements/v1', 'requirements':item['requirements']})


def current_records(records, item):
    identity = semantic_plan_id(item)
    fields = ('query','required','kind','place','duration','orientation','purpose')
    for record in records:
        # Upgrade earlier complete requests only with explicit submission proof.
        if not record.get('semantic_plan_id') and record.get('request_fingerprint') and all(
                record.get('request', {}).get(k) == item['request'].get(k) for k in fields):
            record['semantic_plan_id'] = identity
    return [record for record in records if record.get('semantic_plan_id') == identity]


def load_plan(root, analysis, technical):
    directory = Path(root) / SKILL_DIR
    policy = json.loads((directory/'references/material-search-policy.json').read_text())
    spec = importlib.util.spec_from_file_location('reference_search_planner', directory/'scripts/plan_material_search.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.validate_policy(policy)
    plan = module.build_plan(analysis, technical, policy)
    plan['skill_sha256'] = hashlib.sha256((directory/'SKILL.md').read_bytes()).hexdigest()
    return policy, plan


def task_records(job, sid):
    """All attempts, including migrated tasks; never discard older candidates."""
    records = list(job.get('material_search', {}).get('shots', {}).get(sid, {}).get('task_records', []))
    records += job.get('legacy_search_history', {}).get(sid, [])
    if job.get('legacy_search_tasks', {}).get(sid):
        records.append(job['legacy_search_tasks'][sid])
    by_id = {}
    for row in records:
        tid = row.get('task_id')
        if type(tid) is int:
            by_id.setdefault(tid, row)
    return list(by_id.values())


def related_asset_ids(job, sid, root):
    ids = set()
    for record in task_records(job, sid):
        state = legacy.task(record['task_id'], project_root=root)
        ids.update(c['asset'] for c in state.get('candidates', []) if c.get('asset'))
    return ids


def _failure(state):
    if state.get('state') not in ('partial', 'failed'):
        return ''  # A completed task can retain nonfatal supplemental warnings.
    result = state.get('result', {})
    failure = result.get('failure') or {}
    text = ' '.join(str(x) for x in result.get('errors', []))
    category = failure.get('category') or state.get('outcome')
    if category in ('rate_limited', 'rate_limit') or '429' in text or '503' in text:
        return 'rate_limited'
    if category in ('network', 'network_error', 'timeout', 'source_unavailable') or any(t in text.lower() for t in
            ('timeout', 'timed out', 'ssl', 'connection', '502', '504', '网络', '连接', '超时')):
        return 'network'
    return ''


def _waiting_time(state, record, policy, now):
    result = state.get('result', {})
    failure = result.get('failure') or {}
    count = int(result.get('network_retries', 0))
    retry = policy['network_retry']
    observed = record.setdefault('failure_observed', {})
    record.setdefault('network_wait_started_at', now)
    key = str(count) + ':' + str(failure.get('cooldown_until', ''))
    since = observed.setdefault(key, now)
    delay = min(retry['max_delay_seconds'], retry['base_delay_seconds'] * retry['multiplier'] ** count)
    server_until = float(failure.get('cooldown_until') or result.get('cooldown_until') or state.get('cooldown_until') or 0)
    server_after = float(failure.get('retry_after_seconds') or result.get('retry_after_seconds') or state.get('retry_after_seconds') or 0)
    return max(since + delay, server_until, since + server_after)


def _failure_source(failure):
    name = str(failure.get('source') or '').strip().casefold()
    return {'wikimedia commons':'Wikimedia Commons', 'internet archive':'Internet Archive'}.get(name, 'unknown')


def _shared_cooldowns(task, record, policy, now):
    """Keep every historical provider deadline without merging unrelated sources."""
    result = task.get('result', {})
    waits = {}
    current = result.get('failure') or {}
    if _failure(task):
        waits[_failure_source(current)] = _waiting_time(task, record, policy, now)
    # A completed/resumed task can still carry a provider deadline. A second
    # source can also be cooling while the task's primary failure is elsewhere.
    for failure in (current, result.get('last_failure'), result.get('supplemental', {}).get('failure')):
        if not failure:
            continue
        until = float(failure.get('cooldown_until') or 0)
        after = float(failure.get('retry_after_seconds') or 0)
        occurred = float(failure.get('occurred_at') or 0)
        if after and occurred:
            until = max(until, occurred + after)
        elif after and not until:
            synthetic = {'state':'failed', 'result':{'failure':failure,
                'network_retries':result.get('network_retries',0)}}
            until = _waiting_time(synthetic, record, policy, now)
        source = _failure_source(failure)
        waits[source] = max(waits.get(source, 0), until)
    return waits


def _source_wait(waits, source):
    # Missing/unrecognized origin could be either provider, so it must not
    # bypass any known deadline. Known providers share only their own cooldown.
    if source == 'unknown':
        return max(waits.values(), default=0)
    return max(waits.get(source, 0), waits.get('unknown', 0))


def candidate_items(job, sid, root):
    found = {}
    for record in task_records(job, sid):
        state = legacy.task(record['task_id'], project_root=root)
        for item in state.get('candidate_items', []):
            found[item['id']] = item
    return list(found.values())


def tick(service, job, *, now=None):
    """One short scheduling step. No sleep, no approval, no visual-match claim."""
    now = time.time() if now is None else now
    policy, plan = load_plan(service.root, job['analysis'], job['technical'])
    directory = service.directory(job['id'])
    (directory/'material-search-plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2))
    state = job.setdefault('material_search', {'schema':'reference-material-search/v1', 'shots':{},
        'started_at':now, 'cooldown_until':0})
    state.update(policy_sha256=digest(policy), skill_sha256=plan['skill_sha256'], enabled=True)
    gaps = {gap['shot_id']:gap for gap in job['gaps']}
    planned = {row['shot_id']:row for row in plan['shots']}
    snapshots, active, source_waits = {}, 0, {}
    for sid in gaps:
        service.check(job)
        row = state['shots'].setdefault(sid, {})
        permanent_error = row.get('permanent_error')
        row['task_records'] = task_records(job, sid)
        row.pop('reconciliation_error', None)
        pending = row.get('pending_submission')
        if pending:
            try:
                found = legacy.find_request(service.root, pending['request_fingerprint'])
                if found:
                    restored = {'task_id':found['id'], 'provider':'legacy-commons',
                        'request':pending['request'], 'request_fingerprint':pending['request_fingerprint'],
                        'semantic_plan_id':pending.get('semantic_plan_id') or semantic_plan_id(planned[sid]),
                        'kind':pending['request']['kind'], 'mode':pending['request']['mode']}
                    if not any(r['task_id'] == found['id'] for r in row['task_records']):
                        row['task_records'].append(restored)
                    job.setdefault('legacy_search_tasks', {})[sid] = restored
                    job.setdefault('legacy_search_history', {})[sid] = row['task_records'][:]
                    row.pop('pending_submission', None)
                    service.save(job)
            except legacy.LegacyError as exc:
                row['reconciliation_error'] = str(exc)[:300]
                active += 1
        row['attempts'] = len(row['task_records'])
        row['request_fingerprint'] = planned[sid]['request_fingerprint']
        row['semantic_plan_id'] = semantic_plan_id(planned[sid])
        row['plan_attempts'] = len(current_records(row['task_records'], planned[sid]))
        row['status'] = 'checking'
        row.pop('next_retry_at', None)
        row.pop('retry_reason', None)
        if planned[sid].get('blockers'):
            row.update(status='blocked', reason=json.dumps(planned[sid]['blockers'], ensure_ascii=False))
            continue
        if permanent_error and permanent_error.get('plan') == planned[sid]['request_fingerprint']:
            row.update(status='blocked', reason=permanent_error['reason'])
            continue
        for record in row['task_records']:
            try:
                task = legacy.task(record['task_id'], project_root=service.root)
                snapshots[record['task_id']] = task
                if task['state'] in ACTIVE:
                    active += 1
                for source, until in _shared_cooldowns(task, record, policy, now).items():
                    source_waits[source] = max(source_waits.get(source, 0), until)
            except (legacy.LegacyError, OSError, ValueError) as exc:
                row.update(status='connection_error', reason=str(exc)[:300], next_retry_at=now+15)
        row['candidate_items'] = []
        for record in row['task_records']:
            row['candidate_items'] += snapshots.get(record['task_id'], {}).get('candidate_items', [])
        row['candidate_items'] = list({str(c['id']):c for c in row['candidate_items']}.values())
    state['source_cooldowns'] = source_waits
    state['cooldown_until'] = max(source_waits.values(), default=0)
    service.save(job, status='awaiting_materials', stage='按分镜跟踪旧素材库搜索与候选')

    try:
        state['service'] = legacy.ensure_running(service.root, service.out)
    except legacy.LegacyError as exc:
        state.update(state='connection_error', reason=str(exc)[:300], next_retry_at=now+15)
        service.save(job, status='awaiting_materials', stage=state['reason'])
        return state
    approved_assets = {c['asset_id'] for row in state['shots'].values()
        for c in row.get('candidate_items', []) if c.get('approved') and c.get('asset_id')}
    exported = legacy.export_approved_videos(service.root, asset_ids=approved_assets) if approved_assets else []
    for sid, gap in gaps.items():
        service.check(job)
        row, item = state['shots'][sid], planned[sid]
        if row.get('status') == 'blocked':
            continue
        if row.get('reconciliation_error'):
            row.update(status='connection_error', reason=row['reconciliation_error'], next_retry_at=now+15)
            continue
        records = row['task_records']
        attempts = current_records(records, item)
        if any(r['task_id'] not in snapshots for r in records):
            continue  # Unknown submission state is not permission to submit again.
        if any(snapshots[r['task_id']]['state'] in ACTIVE for r in records):
            row.update(status='searching', reason='旧素材库正在执行搜索')
            continue
        items = row['candidate_items']
        asset_ids = {c.get('asset_id') for c in items if c.get('approved')}
        eligible = [c for c in exported if c.get('legacy_asset_id') in asset_ids
            and c['id'] not in job.get('rejected', {}).get(sid, [])]
        if eligible:
            row.update(status='ready_to_match', reason='已有已审素材，等待按原分镜核验')
            continue
        reviews = state.get('content_reviews', {}).get(sid, {})
        downloaded = [c for c in items if c.get('downloaded') and not c.get('approved') and c.get('kind','video') == 'video']
        if any(reviews.get(c.get('asset_id'), {}).get('status') == 'content_match' for c in downloaded):
            row.update(status='pending_approval', reason='候选画面初筛通过，等待真实审批；尚未作为成片素材')
            continue
        def pending_content(candidate):
            result = reviews.get(candidate.get('asset_id'), {})
            return result.get('status') not in ('rejected','content_match','deferred') and not (
                result.get('status') == 'error' and result.get('error_count',0) >= 2)
        if any(pending_content(c) for c in downloaded):
            row.update(status='content_review', reason='已下载候选，正在核对实际画面与分镜要求')
            continue
        unknown = [c for c in downloaded if reviews.get(c.get('asset_id'), {}).get('status') in ('error','deferred')]
        if unknown:
            row['review_warning'] = '部分候选内容未确认，已保留错误或审核预算记录；继续寻找其他候选'
        pending_failure = attempts[-1] if attempts and _failure(snapshots[attempts[-1]['task_id']]) else None
        if pending_failure:
            task = snapshots[pending_failure['task_id']]
            retries = int(task.get('result', {}).get('network_retries', 0))
            source = _failure_source(task.get('result', {}).get('failure') or {})
            until = max(_source_wait(source_waits, source), _waiting_time(task, pending_failure, policy, now))
            if retries >= policy['network_retry']['max_retries']:
                row.update(status='failed', reason='网络恢复次数已用完；保留原任务和候选')
                continue
            if now-pending_failure['network_wait_started_at'] > policy['network_retry']['max_total_wait_seconds']:
                row.update(status='deferred', reason='自动等待预算已用完，保留原任务，可继续恢复', next_retry_at=until)
                continue
            row.update(status='cooldown', reason='来源限流或连接失败，等待恢复同一个搜索任务',
                retry_reason=_failure(task), next_retry_at=until)
            if until > now or active >= policy['max_concurrent_tasks']:
                continue
            try:
                legacy.resume_search(pending_failure['task_id'])
                row.update(status='searching', reason='已恢复原搜索任务；已有候选保留')
                active += 1
            except legacy.LegacyError as exc:
                row['reason'] = str(exc)[:300]
                row['next_retry_at'] = max(until, float(getattr(exc, 'cooldown_until', 0) or 0),
                    now+float(getattr(exc, 'retry_after_seconds', 15) or 15))
                if getattr(exc, 'retryable', True) is False:
                    row.update(status='blocked', permanent_error={'plan':item['request_fingerprint'], 'reason':str(exc)[:300]})
            service.save(job)
            continue
        if records and any(snapshots[r['task_id']]['state'] not in ('done','partial','failed') for r in records):
            row.update(status='failed', reason='来源任务尚未明确结束，保留任务编号')
            continue
        queries = item.get('recall_queries', item.get('english_queries', []))
        pending = row.get('pending_submission')
        if not pending and len(attempts) >= policy['max_external_tasks_per_shot']:
            row.update(status='exhausted', reason='本轮搜索次数已用完，仍未确认合格视频；保留候选与核验记录')
            continue
        commons_wait = _source_wait(source_waits, 'Wikimedia Commons')
        if commons_wait > now:
            row.update(status='cooldown', next_retry_at=commons_wait, retry_reason='Commons或未知来源共享冷却', reason='等待主搜索来源恢复后提交')
            continue
        if active >= policy['max_concurrent_tasks']:
            row.update(status='queued', reason='等待前一批搜索完成')
            continue
        request = dict(item['request'])
        if attempts and not pending:
            used = {' '.join(str(r.get('request', {}).get('keywords','')).casefold().split()) for r in attempts}
            revised = next((q for q in queries if ' '.join(q.casefold().split()) not in used), None)
            if not revised:
                row.update(status='exhausted', reason='没有新的有效搜索词；保留缺口与来源记录')
                continue
            request['keywords'] = revised
        request['mode'] = 'agent' if legacy.config(service.root)['agent_configured'] and request['kind']=='video' else 'fixed'
        context = {'job_id':job['id'], 'shot_id':sid, 'attempt':len(attempts)+1}
        fingerprint = digest({'request':request, 'plan':item['request_fingerprint'], 'context':context})
        request.update(request_fingerprint=fingerprint, reference_context=context)
        if pending:
            request = pending['request']
            fingerprint = pending['request_fingerprint']
        submission_plan = pending.get('semantic_plan_id', semantic_plan_id(item)) if pending else semantic_plan_id(item)
        row['pending_submission'] = {'request_fingerprint':fingerprint, 'request':request,
            'semantic_plan_id':submission_plan}
        service.save(job)  # Stable id is durable before the HTTP mutation.
        try:
            existing = legacy.find_request(service.root, fingerprint)
            submitted = ({'task_id':existing['id'], 'provider':'legacy-commons', 'kind':request['kind'], 'mode':request['mode']}
                         if existing else legacy.search(request))
            record = {**submitted, 'request_fingerprint':fingerprint, 'request':request,
                'semantic_plan_id':submission_plan}
            if not any(r['task_id'] == record['task_id'] for r in records):
                records.append(record)
            job.setdefault('legacy_search_tasks', {})[sid] = record
            job.setdefault('legacy_search_history', {})[sid] = records[:]
            row.update(attempts=len(records), plan_attempts=len(attempts)+1, status='searching', reason='已交给旧素材库搜索 Agent')
            row.pop('pending_submission', None)
            gap.pop('search_error', None)
            active += 1
        except legacy.LegacyBusy as exc:
            row.update(status='queued', reason=str(exc), next_retry_at=now+policy['poll_interval_seconds'])
        except legacy.LegacyError as exc:
            active += 1  # An unacknowledged submission may already be running.
            row.update(status='connection_error', reason=str(exc)[:300], next_retry_at=now+15)
            if getattr(exc, 'retryable', True) is False:
                row.update(status='blocked', permanent_error={'plan':item['request_fingerprint'], 'reason':str(exc)[:300]})
                row.pop('pending_submission', None)
            gap['search_error'] = str(exc)[:300]
        service.save(job)
    statuses = [state['shots'][sid].get('status') for sid in gaps]
    state['attempts'] = sum(row.get('attempts', 0) for row in state['shots'].values())
    state['state'] = ('cooldown' if 'cooldown' in statuses else 'searching' if 'searching' in statuses else
        'connection_error' if 'connection_error' in statuses else 'queued' if 'queued' in statuses else
        'content_review' if 'content_review' in statuses else 'pending_approval' if 'pending_approval' in statuses else 'finished')
    state['reason'] = f"{len(gaps)} 个镜头缺口；{sum(s=='searching' for s in statuses)} 个搜索执行中"
    due = [row['next_retry_at'] for row in state['shots'].values() if row.get('next_retry_at')]
    state['next_retry_at'] = min(due) if due else None
    service.save(job, status='awaiting_materials', stage=state['reason'])
    return state
