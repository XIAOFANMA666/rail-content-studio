"""Idempotent search submission and bounded recovery of source GET failures.

Model planning is never a recovery target. Completed rounds and candidates stay
in place; only an identified source operation is retried.
"""
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import math
import re
import socket
import ssl
import time
import urllib.error


MAX_NETWORK_RETRIES = 3


class CooldownError(ValueError):
    def __init__(self, job_id, until, now):
        super().__init__('来源正在冷却，请稍后恢复搜索')
        self.payload = dict(error=str(self), id=job_id,
                            retry_after_seconds=max(1, math.ceil(until-now)), cooldown_until=until)


def external_metadata(body):
    fingerprint = body.get('request_fingerprint')
    if fingerprint is None:
        return {}
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', fingerprint):
        raise ValueError('request_fingerprint 必须是 64 位十六进制字符串')
    result = {'external_request_id': fingerprint.lower()}
    context = body.get('reference_context')
    if context is not None:
        if not isinstance(context, dict):
            raise ValueError('reference_context 必须是对象')
        clean = {}
        for name in ('job_id', 'shot_id'):
            if name in context:
                if not isinstance(context[name], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', context[name]):
                    raise ValueError('reference_context 标识无效')
                clean[name] = context[name]
        if 'attempt' in context:
            if type(context['attempt']) is not int or not 0 <= context['attempt'] <= 100:
                raise ValueError('reference_context.attempt 无效')
            clean['attempt'] = context['attempt']
        result['reference_context'] = clean
    return result


def existing_request(conn, metadata):
    fingerprint = metadata.get('external_request_id')
    if fingerprint:
        for row in conn.execute('SELECT id,payload FROM commons_job ORDER BY id'):
            if json.loads(row['payload']).get('external_request_id') == fingerprint:
                return row['id']
    return None


def classification(exc):
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return 'rate_limit'
        if exc.code in (408, 500, 502, 503, 504):
            return 'source_unavailable'
        return None
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    message = str(reason)
    if isinstance(reason, ssl.SSLCertVerificationError) or re.search(
            r'CERT_|CERTIFICATE|SELF_SIGNED|UNABLE_TO_VERIFY|certificate verify', message, re.I):
        return None
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return 'timeout'
    if isinstance(exc, urllib.error.URLError) or isinstance(reason, (ConnectionError, socket.gaierror, ssl.SSLError)):
        return 'network'
    return None


def retry_after(value, now):
    try:
        seconds = float(value)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (TypeError, ValueError):
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, date.timestamp()-now)
        except (TypeError, ValueError, OverflowError):
            return 0


def record_failure(counts, exc, *, source='Wikimedia Commons', target=None, now=None):
    category = classification(exc)
    if not category:
        return None
    now = time.time() if now is None else now
    headers = getattr(exc, 'headers', None)
    supplied = retry_after(headers.get('Retry-After') if headers else None, now)
    delay = max(min(900, 60 * 2 ** min(int(counts.get('network_retries', 0)), 4)), supplied)
    failure = dict(category=category, source=source, status=getattr(exc, 'code', None),
                   retry_after_seconds=math.ceil(delay), cooldown_until=now+delay,
                   occurred_at=now, message=str(exc)[:240], target=target or {})
    counts['failure'] = failure
    counts['retry_after_seconds'] = failure['retry_after_seconds']
    counts['cooldown_until'] = failure['cooldown_until']
    return failure


def legacy_failure(body, counts, now):
    """Conservatively recover old rows whose saved source round proves failure."""
    rounds = counts.get('rounds', [])
    if body.get('mode') == 'agent':
        if not rounds or rounds[-1].get('action') not in ('search', 'version'):
            return None
        message = rounds[-1].get('result', '')
        if message not in counts.get('errors', []):
            return None
        target = {'stage': 'commons', 'round_index': len(rounds)-1}
    elif len(body.get('queries', [])) == 1 and counts.get('errors'):
        message = counts['errors'][-1]
        target = {'stage': 'commons', 'query_index': 0}
    else:
        return None
    status = re.search(r'HTTP Error (429|408|500|502|503|504)\b', message)
    if status:
        exc = urllib.error.HTTPError('', int(status[1]), '历史来源请求失败', None, None)
    elif re.search(r'ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|网络请求超时|<urlopen error', message):
        exc = urllib.error.URLError(message)
    else:
        return None
    return record_failure(counts, exc, target=target, now=now)


def source_cooldown(conn, source):
    until = 0
    for row in conn.execute("SELECT result FROM commons_job WHERE result LIKE '%cooldown_until%'"):
        result = json.loads(row['result'])
        for failure in (result.get('failure'), result.get('last_failure'), result.get('supplemental', {}).get('failure')):
            if failure and failure.get('source') == source:
                until = max(until, float(failure.get('cooldown_until', 0)))
    return until


def next_queued(conn, now=None):
    """A provider's cooldown also protects the other queued search tasks."""
    now = time.time() if now is None else now
    limits = {}
    for row in conn.execute("SELECT * FROM commons_job WHERE state='queued' ORDER BY id"):
        counts = json.loads(row['result'])
        source = ('Internet Archive' if counts.get('resume_target', {}).get('stage') == 'supplemental'
                  else 'Wikimedia Commons')
        if source not in limits:
            limits[source] = source_cooldown(conn, source)
        if limits[source] <= now:
            return row
    return None


def resume_search(conn, job_id, now=None):
    """Must run inside the caller's BEGIN IMMEDIATE transaction."""
    if type(job_id) is not int or job_id <= 0:
        raise ValueError('任务编号无效')
    now = time.time() if now is None else now
    row = conn.execute('SELECT * FROM commons_job WHERE id=?', (job_id,)).fetchone()
    if not row:
        raise ValueError('搜索任务不存在')
    counts, body = json.loads(row['result']), json.loads(row['payload'])
    retries = int(counts.get('network_retries', 0))
    if row['state'] in ('queued', 'running'):
        return dict(id=job_id, state=row['state'], reused=True, network_retries=retries)
    if row['state'] not in ('partial', 'failed') or 'retryPage' in body:
        raise ValueError('只有来源网络失败的搜索任务可以恢复')
    if retries >= MAX_NETWORK_RETRIES:
        raise ValueError('该搜索任务已达到 3 次网络恢复上限')
    failure = counts.get('failure') or legacy_failure(body, counts, now)
    if not failure or failure.get('category') not in ('rate_limit', 'source_unavailable', 'network', 'timeout'):
        raise ValueError('该任务没有可恢复的来源网络错误')
    # Persist migration before returning a cooldown response, so polling cannot
    # repeatedly start a fresh cooldown for historical tasks.
    conn.execute('UPDATE commons_job SET result=? WHERE id=?', (json.dumps(counts, ensure_ascii=False), job_id))
    until = max(float(failure.get('cooldown_until', 0)), source_cooldown(conn, failure['source']))
    if until > now:
        raise CooldownError(job_id, until, now)
    if conn.execute("SELECT count(*) FROM commons_job WHERE state IN ('queued','running')").fetchone()[0] >= 5:
        raise ValueError('已有 5 个任务在处理，请稍后再试')
    target = failure.get('target', {})
    if target.get('stage') == 'commons' and body.get('mode') == 'agent':
        index = target.get('round_index')
        rounds = counts.get('rounds', [])
        if type(index) is not int or not 0 <= index < len(rounds) or index != len(rounds)-1:
            raise ValueError('无法定位失败的来源查询轮次')
        rounds[index]['complete'] = False
    elif target.get('stage') == 'commons':
        index = target.get('query_index')
        if type(index) is not int or not 0 <= index < len(body.get('queries', [])):
            raise ValueError('无法定位失败的来源查询')
        counts['cursor'] = index
    elif target.get('stage') == 'supplemental':
        counts.setdefault('supplemental', {})['complete'] = False
    else:
        raise ValueError('该网络错误不是可恢复的搜索步骤')
    counts.setdefault('error_history', []).append(dict(at=now, errors=counts.get('errors', []),
        failure=failure, stopReason=counts.get('stopReason', '')))
    counts.update(errors=[], network_retries=retries+1, resume_target=target,
                  startedAt=now, last_failure=failure)
    for key in ('failure', 'stopReason', 'supplementStartedAt', 'supplementSeconds'):
        counts.pop(key, None)
    conn.execute("UPDATE commons_job SET state='queued',result=? WHERE id=?",
                 (json.dumps(counts, ensure_ascii=False), job_id))
    return dict(id=job_id, state='queued', reused=False, network_retries=retries+1)
