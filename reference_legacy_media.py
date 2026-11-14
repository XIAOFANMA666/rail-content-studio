"""Bridge to the existing local image/video library without resetting its database.

Search uses its actual HTTP queue. Inventory reads SQLite in read-only mode and
exports only reviewed video intervals; an image can never fill a motion shot.
"""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

DEFAULT_URL = 'http://127.0.0.1:8765'
_START_LOCK = threading.Lock()
_VIDEO_EXTENSIONS = {'.mp4', '.mov', '.m4v', '.webm', '.ogv'}
_VIDEO_DEMUXERS = {'.mp4': 'mov', '.mov': 'mov', '.m4v': 'mov', '.webm': 'matroska', '.ogv': 'ogg'}


class LegacyError(ValueError):
    def __init__(self, message, *, http_status=None, retry_after_seconds=None, cooldown_until=None, task_id=None,
                 retryable=False):
        super().__init__(message)
        self.http_status = self.status_code = http_status
        self.retry_after_seconds = retry_after_seconds
        self.cooldown_until = cooldown_until
        self.task_id = task_id
        self.retryable = bool(retryable)


class LegacyBusy(LegacyError):
    pass


def legacy_root(project_root, root=None):
    return Path(root).resolve() if root else Path(project_root).resolve().parent / 'media-library'


def _base_url(value):
    try:
        parsed = urlsplit(value)
        loopback = parsed.hostname == 'localhost' or ipaddress.ip_address(parsed.hostname).is_loopback
        if (parsed.scheme != 'http' or not loopback or parsed.username or parsed.password
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment or not parsed.port):
            raise ValueError()
    except (ValueError, TypeError):
        raise LegacyError('旧素材库地址必须是带端口的本机 HTTP 地址') from None
    return value.rstrip('/')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(path, body=None, *, base_url=DEFAULT_URL, timeout=12):
    base = _base_url(base_url)
    headers = {'Accept': 'application/json', 'Origin': base}
    blob = None
    if body is not None:
        blob = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        if len(blob) > 65536:
            raise LegacyError('旧素材库请求超过 64 KB，请精简请求；要求未被裁剪或提交', http_status=400)
        headers['Content-Type'] = 'application/json'
    request = urllib.request.Request(base + path, data=blob, headers=headers)
    deadline = time.monotonic() + timeout
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            chunks, size = [], 0
            while size <= 4 * 1024 * 1024:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                try:
                    response.fp.raw._sock.settimeout(remaining)
                except AttributeError:
                    pass
                chunk = response.read1(min(65536, 4 * 1024 * 1024 + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            data = b''.join(chunks)
        if len(data) > 4 * 1024 * 1024:
            raise LegacyError('旧素材库返回内容过大')
        result = json.loads(data)
    except urllib.error.HTTPError as exc:
        try:
            payload = _object(json.loads(exc.read(16384))) if body is not None else {}
        except (OSError,ValueError,AttributeError):
            payload = {}
        finally:
            exc.close()
        error = payload.get('error', '')
        if exc.code==400 and error=='已有 5 个任务在处理，请稍后再试':
            raise LegacyBusy('素材库正在处理前一批任务，余下镜头将自动排队', http_status=exc.code, retryable=True) from None
        task_id = payload.get('id')
        raise LegacyError('旧素材库请求失败（HTTP %s）' % exc.code,
            http_status=exc.code, task_id=task_id if type(task_id) is int and task_id > 0 else None,
            retryable=exc.code in (408, 429, 500, 502, 503, 504),
            **_cooldown_values(payload)) from None
    except LegacyError:
        raise
    except (OSError, ValueError):
        raise LegacyError('无法连接旧素材库，请先启动本机素材库服务', retryable=True) from None
    return result


def _health(timeout):
    try:
        status = _request('/api/commons/agent-status', timeout=timeout)
    except LegacyError:
        return None
    if not isinstance(status, dict) or type(status.get('configured')) is not bool:
        return None
    required = ('provider', 'model') if status['configured'] else ('message',)
    if not all(isinstance(status.get(key), str) and status[key].strip() for key in required):
        return None
    # Only the public status contract is returned, even if another server sends
    # unexpected keys. This endpoint must never expose model credentials.
    return {key: status[key] for key in ('configured', *required)}


def _port_open(timeout):
    try:
        with socket.create_connection(('127.0.0.1', 8765), timeout=timeout):
            return True
    except OSError:
        return False


def _pid_running(pid):
    if type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def ensure_running(project_root, out, *, root=None):
    """Health-check/start the sibling library only for an explicit search action.

    Startup uses this interpreter and a separate log in ``out``. A process and
    thread lock prevents duplicate launches, including after a slow startup.
    The health request, lock acquisition, and startup share a five-second budget.
    Reading ``config()`` never invokes this function.
    """
    import fcntl

    deadline = time.monotonic() + 5
    old = legacy_root(project_root, root)

    def remaining():
        return max(0, deadline - time.monotonic())

    def check():
        wait = remaining()
        return _health(min(.35, wait)) if wait > 0 else None

    status = check()
    if status is not None:
        return {'running': True, 'started': False, 'base_url': DEFAULT_URL, 'agent': status}
    if not (old / 'server.py').is_file():
        raise LegacyError('未找到本机旧素材库服务程序')
    lock_name = 'rail-legacy-start-' + hashlib.sha256(str(old).encode()).hexdigest()[:20] + '.lock'
    if not _START_LOCK.acquire(timeout=remaining()):
        raise LegacyError('旧素材库正在启动，请稍后重试')
    try:
        with (Path(tempfile.gettempdir()) / lock_name).open('a+') as lock:
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not remaining():
                        raise LegacyError('旧素材库正在启动，请稍后重试') from None
                    time.sleep(min(.05, remaining()))
            try:
                status = check()
                if status is not None:
                    return {'running': True, 'started': False, 'base_url': DEFAULT_URL, 'agent': status}
                if not remaining():
                    raise LegacyError('旧素材库在 5 秒内未就绪，请检查本次任务的启动日志')
                if _port_open(min(.2, max(.001, remaining()))):
                    raise LegacyError('8765 端口已有其他服务或素材库尚未就绪，未启动重复进程')
                lock.seek(0)
                try:
                    pid = json.loads(lock.read(256)).get('pid')
                except (ValueError, AttributeError):
                    pid = None
                process = None
                if not _pid_running(pid):
                    if not remaining():
                        raise LegacyError('旧素材库在 5 秒内未就绪，请检查本次任务的启动日志')
                    log_dir = Path(out).resolve()
                    log_dir.mkdir(parents=True, exist_ok=True)
                    with (log_dir / 'legacy-media-server.log').open('ab') as log:
                        try:
                            process = subprocess.Popen([sys.executable, '-B', str(old / 'server.py'), '--port', '8765'],
                                cwd=old, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True)
                        except OSError:
                            raise LegacyError('无法启动旧素材库服务，请检查本机运行环境') from None
                    lock.seek(0)
                    lock.truncate()
                    json.dump({'pid': process.pid}, lock)
                    lock.flush()
                while remaining():
                    status = check()
                    if status is not None:
                        return {'running': True, 'started': process is not None, 'base_url': DEFAULT_URL, 'agent': status}
                    if process is not None and process.poll() is not None:
                        raise LegacyError('旧素材库启动失败，请检查本次任务的启动日志')
                    time.sleep(min(.1, remaining()))
                raise LegacyError('旧素材库在 5 秒内未就绪，请检查本次任务的启动日志')
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except OSError:
        raise LegacyError('无法准备旧素材库启动，请检查本机目录权限') from None
    finally:
        _START_LOCK.release()


def config(project_root, *, root=None):
    """Local presence only; never starts a server, resets jobs, or returns keys."""
    old = legacy_root(project_root, root)
    agent = old / 'search-agent.local.json'
    configured = False
    try:
        value = json.loads(agent.read_text())
        configured = not bool(agent.stat().st_mode & 0o077) and all(
            isinstance(value.get(k), str) and value[k].strip()
            for k in ('provider', 'model', 'base_url', 'api_key'))
    except (OSError, ValueError, TypeError):
        pass
    return {'available': (old / 'server.py').is_file() and (old / 'data/library.sqlite3').is_file(),
            'commons_images': (old / 'commons.py').is_file(),
            'commons_videos': (old / 'commons.py').is_file(),
            'archive_videos': (old / 'video_sources.py').is_file(),
            'agent_configured': bool(configured)}


def search(request, *, base_url=DEFAULT_URL):
    """Enqueue Commons search; image/video kind is explicit, fixed needs no key.

    request: {query, kind:'video'|'image', keywords?, place?, required?,
              duration?, orientation?:portrait|landscape|square|'',
              purpose?:internal|publish, mode?:fixed|agent}.
    The full description (up to 1000 characters), place (500), and required text
    (4000) are forwarded without truncation. The source's search keyword string has a
    separate 180-character budget; any abbreviation is exposed in the result as
    keywords_shortened=True and never replaces the full script or hard rules.
    The existing server accepts at most 64 KiB of request JSON; oversize requests
    fail explicitly before submission instead of silently dropping conditions.
    Returns {'provider':'legacy-commons','task_id':int,'kind':...}.
    """
    if not isinstance(request, dict):
        raise LegacyError('搜索请求无效')
    query = str(request.get('query', '')).strip()
    place = str(request.get('place', ''))
    required = str(request.get('required', ''))
    kind = request.get('kind', 'video')
    if not 1 <= len(query) <= 1000 or kind not in ('image', 'video'):
        raise LegacyError('请指定 1 至 1000 字符的搜索描述和图片或视频类型', http_status=400)
    if len(place) > 500 or len(required) > 4000:
        raise LegacyError('地点最多 500 字符，必需条件最多 4000 字符；要求未被裁剪或提交', http_status=400)
    mode = request.get('mode', 'fixed')
    purpose = request.get('purpose', 'internal')
    orientation = request.get('orientation', '')
    if mode not in ('fixed', 'agent') or purpose not in ('internal', 'publish') or orientation not in ('', 'portrait', 'landscape', 'square'):
        raise LegacyError('搜索模式、用途或画幅无效')
    raw = request.get('duration', 0)
    try:
        seconds = float(raw)
        if isinstance(raw, bool) or not math.isfinite(seconds) or not 0 <= seconds <= 180:
            raise ValueError()
    except (TypeError, ValueError):
        raise LegacyError('最短时长须在0至180秒内') from None
    if kind == 'image' and seconds:
        raise LegacyError('图片不能满足运动视频的时长要求')
    keywords = str(request.get('keywords') or query).strip()
    keywords_shortened = False
    # Commons recognizes bitmap, not a speculative /images endpoint.
    if kind == 'image':
        keywords = re.sub(r'(?i)(?:^|\s)-?filetype:\S+', ' ', keywords).strip()
        budget = 180 - len(' filetype:bitmap')
        keywords_shortened = len(keywords) > budget
        keywords = keywords[:budget] + ' filetype:bitmap'
        # Agent safe_query rewrites filetype filters: fixed keeps image-only query.
        mode = 'fixed'
    keywords_shortened = keywords_shortened or len(keywords) > 180
    body = {'script': query, 'keywords': keywords[:180], 'videoOnly': kind == 'video',
            'seconds': seconds if kind == 'video' else 0, 'purpose': purpose,
            'orientation': {'': '', 'portrait': '竖屏', 'landscape': '横屏', 'square': '方形'}[orientation],
            'location': place, 'required': required,
            'infer': False, 'mode': mode, 'direct': True}
    fingerprint = request.get('request_fingerprint')
    if fingerprint is not None:
        body['request_fingerprint'] = _fingerprint(fingerprint)
    context = request.get('reference_context')
    if context is not None:
        if not isinstance(context, dict):
            raise LegacyError('参考视频任务上下文无效')
        clean = {}
        for key in ('job_id', 'shot_id'):
            if key in context:
                value = str(context[key])
                if not isinstance(context[key], str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
                    raise LegacyError('参考视频任务标识无效')
                clean[key] = value
        if 'attempt' in context:
            attempt = context['attempt']
            if type(attempt) is not int or not 0 <= attempt <= 100:
                raise LegacyError('参考视频搜索次数无效')
            clean['attempt'] = attempt
        body['reference_context'] = clean
    result = _request('/api/commons/start', body, base_url=base_url)
    if not isinstance(result, dict) or type(result.get('id')) is not int:
        raise LegacyError('旧素材库未返回有效任务编号')
    value = {'provider': 'legacy-commons', 'task_id': result['id'], 'kind': kind, 'mode': mode}
    if keywords_shortened:
        value['keywords_shortened'] = True
    if fingerprint is not None:
        value.update(request_fingerprint=body['request_fingerprint'], reused=result.get('reused') is True)
    return value


def resume_search(task_id, *, base_url=DEFAULT_URL):
    """Resume one recorded source network failure without creating a new job.

    HTTP 429 raises LegacyError with http_status/status_code,
    retry_after_seconds, cooldown_until, and task_id for the scheduler. Other
    errors retain the same safe wrapping as search(); no raw error body escapes.
    """
    if type(task_id) is not int or task_id < 1:
        raise LegacyError('任务编号无效')
    result = _request('/api/commons/resume-search', {'id': task_id}, base_url=base_url)
    if (not isinstance(result, dict) or type(result.get('id')) is not int or result['id'] != task_id
            or result.get('state') not in ('queued', 'running') or type(result.get('reused')) is not bool
            or type(result.get('network_retries')) is not int or result['network_retries'] < 0):
        raise LegacyError('旧素材库未返回有效搜索恢复状态')
    return {'provider': 'legacy-commons', 'task_id': task_id, 'state': result['state'],
            'reused': result['reused'], 'network_retries': result['network_retries'],
            'resumed': not result['reused']}


def _fingerprint(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{64}', value):
        raise LegacyError('搜索请求指纹必须是 64 位十六进制字符')
    return value.lower()


def find_request(project_root, fingerprint, *, root=None):
    """Read an earlier submission after an ambiguous POST response, or None.

    Server contract: POST accepts ``request_fingerprint`` and bounded
    ``reference_context``; within the insert transaction it returns an existing
    matching job, or stores the value in payload.external_request_id. The bridge
    never creates a job here and cannot infer identity from query text alone.
    """
    fingerprint = _fingerprint(fingerprint)
    dbpath = legacy_root(project_root, root) / 'data/library.sqlite3'
    if not dbpath.is_file():
        return None
    try:
        with closing(sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True)) as db:
            # Avoid requiring the optional SQLite JSON extension in old installs.
            rows = db.execute('SELECT id,payload FROM commons_job WHERE payload LIKE ? ORDER BY id',
                              ('%' + fingerprint + '%',))
            found = next((row[0] for row in rows if _object(row[1]).get('external_request_id') == fingerprint), None)
        return task(found, project_root=project_root, root=root) if found is not None else None
    except sqlite3.Error:
        raise LegacyError('无法读取旧素材库任务记录') from None


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except (ValueError, TypeError):
        return {}


def _candidate_kind(candidate):
    p = _object(candidate.get('payload'))
    mime = str(p.get('mime', '')).lower()
    if mime.startswith('image/'):
        return 'image'
    if mime.startswith('video/') and mime != 'video/unknown':
        return 'video'
    if mime == 'application/ogg' and str(p.get('title', '')).lower().endswith('.ogv'):
        return 'video'
    if p.get('discovery') and not p.get('mediaUrl'):
        return 'web'
    return 'unknown'


def _source_url(value):
    """Only expose normal public source links, never URL credentials."""
    if not isinstance(value, str) or len(value) > 4096:
        return ''
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return ''
        if re.search(r'(?i)(?:api[_-]?key|access[_-]?token|authorization|signature|credential)=', parsed.query):
            return parsed._replace(query='', fragment='').geturl()
        return value
    except ValueError:
        return ''


def candidate_summary(candidate, *, db=None, purpose='internal'):
    """Summarize one source; readiness/approval require read-only DB evidence.

    A source page or a candidate.asset link alone is never proof of a download.
    ``approved`` means at least one ready asset interval has a real approved
    segment with the same authorization requirements as inventory export.
    """
    if purpose not in ('internal', 'publish'):
        raise LegacyError('用途无效')
    p = _object(candidate.get('payload'))
    kind = _candidate_kind(candidate)
    asset_id = candidate.get('asset') or None
    downloaded = approved = False
    if db is not None and asset_id and kind in ('video', 'image'):
        asset = db.execute('SELECT status,extension,info FROM asset WHERE id=?', (asset_id,)).fetchone()
        if asset:
            status, extension, info_raw = tuple(asset)
            extension_kind = ('image' if str(extension).lower() in ('.jpg', '.jpeg', '.png', '.webp', '.heic')
                              else 'video' if str(extension).lower() in ('.mp4', '.mov', '.m4v', '.webm', '.ogv') else 'unknown')
            downloaded = status == 'ready' and extension_kind == kind
            if downloaded:
                info = _object(info_raw)
                for row in db.execute("SELECT payload FROM segment WHERE asset=? AND state='approved'", (asset_id,)):
                    segment = _object(row[0])
                    if (segment.get('authorizationVerified') is not True or not segment.get('license')
                            or not segment.get('usage') or (purpose == 'publish' and segment.get('publicationApproved') is not True)):
                        continue
                    if kind == 'video':
                        try:
                            start, end, duration = (float(segment['start']), float(segment['end']), float(info['duration']))
                            if not all(math.isfinite(n) for n in (start, end, duration)) or not 0 <= start < end <= duration + .000001:
                                continue
                        except (KeyError, TypeError, ValueError):
                            continue
                    approved = True
                    break
    return {'id': candidate.get('page_id'), 'title': str(p.get('title') or '')[:1000], 'kind': kind,
            'state': str(candidate.get('state') or 'unknown'), 'asset_id': asset_id,
            'source_url': _source_url(p.get('discovery') or p.get('mediaUrl')),
            'downloaded': downloaded, 'approved': approved}


def _task_outcome(item, candidates):
    result = _object(item.get('result'))
    errors = list(result.get('errors', [])) if isinstance(result.get('errors'), list) else []
    errors += [result.get('stopReason', ''), _object(result.get('supplemental')).get('error', '')]
    for record in result.get('rounds', []) if isinstance(result.get('rounds'), list) else []:
        if isinstance(record, dict):
            errors += [record.get('error', ''), record.get('result', '')]
    message = ' '.join(str(error) for error in errors)
    if re.search(r'(?i)\b(?:429|503)\b|rate.?limit|too many requests|限流|请求过多', message):
        outcome = 'rate_limited'
    elif re.search(r'(?i)urlopen|\b(?:EOF|TLS|SSL|ENOTFOUND|ECONN\w*|502|504)\b|network|connect|timed?\s*out|timeout|网络|连接|超时', message):
        outcome = 'network'
    elif item.get('state') in ('queued', 'running'):
        outcome = None
    else:
        outcome = 'complete' if candidates else 'empty'
    return {'outcome': outcome, **_cooldown_values(result)}


def _cooldown_values(result):
    """Return only finite public scheduling values; never forward raw errors."""
    retry = result.get('retry_after_seconds')
    if isinstance(retry, bool) or not isinstance(retry, (int, float)) or not math.isfinite(retry) or retry < 0:
        retry = None
    cooldown = result.get('cooldown_until')
    if isinstance(cooldown, str):
        try:
            parsed = datetime.fromisoformat(cooldown.replace('Z', '+00:00'))
            cooldown = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
        except ValueError:
            cooldown = None
    if isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or cooldown < 0:
        cooldown = None
    if retry is None and cooldown is not None:
        retry = max(0, cooldown - time.time())
    return {'retry_after_seconds': retry, 'cooldown_until': cooldown}


def task(task_id, *, base_url=DEFAULT_URL, project_root=None, root=None, kind=None):
    """Read actual status; local read avoids the HTTP endpoint's 30-job window.

    Pass kind='image' or 'video' to filter mixed source results by actual MIME.
    The old image-search fallback can return both kinds, never assume the query
    filter alone proves media type.
    """
    if type(task_id) is not int or task_id < 1:
        raise LegacyError('任务编号无效')
    if kind not in (None, 'image', 'video'):
        raise LegacyError('媒体类型无效')
    if project_root is not None:
        dbpath = legacy_root(project_root, root) / 'data/library.sqlite3'
        try:
            with closing(sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('SELECT * FROM commons_job WHERE id=?', (task_id,)).fetchone()
                item = {**dict(row), 'payload': _object(row['payload']), 'result': _object(row['result'])} if row else None
                ids = set(item.get('result', {}).get('pageIds', [])) if item else set()
                candidates = [{**dict(c), 'payload': _object(c['payload'])}
                    for c in db.execute('SELECT * FROM commons_candidate') if c['job_id'] == task_id or c['page_id'] in ids]
                purpose = _object(item.get('payload')).get('purpose', 'internal') if item else 'internal'
                summaries = [candidate_summary(c, db=db, purpose=purpose) for c in candidates]
        except sqlite3.Error:
            raise LegacyError('无法读取旧素材库任务状态') from None
    else:
        result = _request('/api/commons', base_url=base_url)
        item = next((j for j in result.get('jobs', []) if j.get('id') == task_id), None)
        ids = set(item.get('result', {}).get('pageIds', [])) if item else set()
        candidates = [c for c in result.get('candidates', []) if c.get('job_id') == task_id or c.get('page_id') in ids]
        summaries = [candidate_summary(c) for c in candidates]
    if item is None:
        raise LegacyError('未找到旧素材库任务；历史任务请通过本地只读状态查询')
    if kind:
        candidates = [c for c in candidates if str(c.get('payload', {}).get('mime', '')).startswith(kind + '/')]
        # Source leads remain visible to callers, but never count as a match or
        # download. Preserve the legacy candidates MIME-filter contract.
        summaries = [c for c in summaries if c['kind'] in (kind, 'web', 'unknown')]
    return {**item, 'provider': 'legacy-commons', 'candidates': candidates, 'candidate_items': summaries,
            **_task_outcome(item, summaries)}


def _content_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def _video_source(old, asset_id, extension):
    """Find hash-identical source bytes, never trust a derived MP4 as original.

    Legacy WebM imports retain source.webm but record extension=.mp4 for their
    generated preview. The asset identity is the original WebM's SHA-256.
    """
    if not isinstance(asset_id, str) or not re.fullmatch(r'[a-f0-9]{64}', asset_id) or extension not in _VIDEO_EXTENSIONS:
        return None
    media_root = (old / 'data/media').resolve()
    folder = (media_root / asset_id).resolve()
    if folder.parent != media_root:
        return None
    names = ['original' + extension]
    if extension in ('.mp4', '.webm'):
        names.insert(0, 'source.webm')
    if extension in ('.mp4', '.ogv'):
        names.insert(0, 'source.ogv')
    found = False
    for name in names:
        path = (folder / name).resolve()
        if path.parent != folder or path.suffix.lower() not in _VIDEO_EXTENSIONS or not path.is_file():
            continue
        found = True
        digest = _content_hash(path)
        if digest == asset_id:
            return path, digest
    if found:
        raise LegacyError('旧素材原文件已变化，请先重新审核')
    return None


def _probe_video_source(path):
    """Inspect a local self-contained movie, excluding still cover-art streams.

    Source videos may exceed the reference upload's 180-second limit. They are
    not copied or admitted to the approved rendering inventory by this check.
    A fixed demuxer and file-only protocols prevent a disguised playlist from
    reading a URL or an unrelated media resource.
    """
    from reference_video import tool
    demuxer = _VIDEO_DEMUXERS[path.suffix.lower()]
    try:
        process = subprocess.run([tool('ffprobe'), '-v', 'error', '-protocol_whitelist', 'file', '-f', demuxer,
            '-show_entries', 'stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration:stream_disposition=attached_pic:format=format_name,duration',
            '-of', 'json', str(path)], capture_output=True, timeout=30)
        if process.returncode or len(process.stdout) > 1024 * 1024:
            raise ValueError()
        data = json.loads(process.stdout)
        video = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'
                      and not s.get('disposition', {}).get('attached_pic')), None)
        if not video or video.get('codec_name') in (None, '', 'unknown'):
            raise ValueError()
        duration = float(data.get('format', {}).get('duration') or video.get('duration'))
        width, height = int(video.get('width', 0)), int(video.get('height', 0))
        if not math.isfinite(duration) or duration <= 0 or min(width, height) <= 0:
            raise ValueError()
        fps = None
        for value in (video.get('avg_frame_rate'), video.get('r_frame_rate')):
            try:
                a, b = str(value).split('/')
                number = float(a) / float(b)
                if math.isfinite(number) and number > 0:
                    fps = number
                    break
            except (TypeError, ValueError, ZeroDivisionError):
                pass
        if fps is None:
            raise ValueError()
        return {'duration': duration, 'width': width, 'height': height, 'fps': fps,
                'codec': video['codec_name'], 'stream_index': video['index'],
                'format': data.get('format', {}).get('format_name', ''),
                'audio': any(s.get('codec_type') == 'audio' for s in data.get('streams', []))}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        raise LegacyError('旧素材没有可验证的视频轨道') from None


def _source_evidence(db, asset_id, tables):
    evidence = []
    if 'commons_candidate' in tables:
        for row in db.execute('SELECT page_id,payload FROM commons_candidate WHERE asset=?', (asset_id,)):
            p = _object(row['payload'])
            evidence.append({'evidence_type': 'source_claim', 'candidate_id': row['page_id'],
                'provider': str(p.get('provider') or 'Wikimedia Commons'), 'title': str(p.get('title') or ''),
                'source_url': _source_url(p.get('discovery')), 'media_url': _source_url(p.get('mediaUrl')),
                'mime': str(p.get('mime') or ''), 'license': str(p.get('license') or ''),
                'license_url': _source_url(p.get('licenseUrl')), 'author': str(p.get('author') or ''),
                'description': str(p.get('description') or ''), 'fetched_at': str(p.get('fetchedAt') or '')})
    if 'source' in tables:
        for row in db.execute('SELECT name,uploader,origin FROM source WHERE asset=?', (asset_id,)):
            evidence.append({'evidence_type': 'import_record', 'title': row['name'],
                             'uploader': row['uploader'], 'source_url': _source_url(row['origin'])})
    return evidence


def downloaded_videos(project_root, asset_ids, root=None):
    """Read ready video candidates for content screening before human approval.

    Each result has asset_id, path, file_sha256/content_hash, duration, extension,
    metadata (measured video stream), asset_metadata, and source_evidence. Source
    claims are not an authorization decision. No approval fields, copies, or DB
    writes are created. Invalid/missing/changed/nonvideo files are excluded.
    """
    if isinstance(asset_ids, (str, bytes)) or asset_ids is None:
        raise LegacyError('请提供待筛选素材编号列表')
    try:
        wanted = set(asset_ids)
    except TypeError:
        raise LegacyError('待筛选素材编号无效') from None
    if any(not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value) for value in wanted):
        raise LegacyError('待筛选素材编号无效')
    old = legacy_root(project_root, root)
    dbpath = old / 'data/library.sqlite3'
    if not wanted or not dbpath.is_file():
        return []
    records = []
    try:
        with closing(sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for row in db.execute("SELECT id,name,extension,info FROM asset WHERE status='ready'"):
                if row['id'] not in wanted:
                    continue
                try:
                    source = _video_source(old, row['id'], row['extension'])
                    if source is None:
                        continue
                    path, digest = source
                    metadata = _probe_video_source(path)
                except (LegacyError, OSError):
                    continue
                records.append({'asset_id': row['id'], 'name': row['name'], 'path': str(path),
                    'file_sha256': digest, 'content_hash': digest, 'duration': metadata['duration'],
                    'extension': path.suffix.lower(), 'metadata': metadata, 'asset_metadata': _object(row['info']),
                    'source_evidence': _source_evidence(db, row['id'], tables)})
    except sqlite3.Error:
        raise LegacyError('无法读取旧素材库待筛选视频') from None
    return records


def approved_videos(project_root, *, root=None, purpose='internal', asset_ids=None):
    """Read reviewed intervals; image/pending/hold/unlicensed rows are excluded.

    Optional asset_ids limits reads to candidate assets linked to one search job.
    This does not import server.py and never initializes or modifies the old DB.
    """
    if purpose not in ('internal', 'publish'):
        raise LegacyError('用途无效')
    old = legacy_root(project_root, root)
    dbpath = old / 'data/library.sqlite3'
    if not dbpath.is_file():
        return []
    rows = []
    with closing(sqlite3.connect(dbpath.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        for row in db.execute("""SELECT a.id asset_id,a.name,a.extension,a.info,
                 s.id segment_id,s.payload,s.reviewer,s.updated FROM asset a JOIN segment s ON s.asset=a.id
                 WHERE a.status='ready' AND s.state='approved'"""):
            if row['extension'] not in _VIDEO_EXTENSIONS or (asset_ids is not None and row['asset_id'] not in asset_ids):
                continue
            p = json.loads(row['payload']); info = json.loads(row['info'])
            if (p.get('authorizationVerified') is not True or not p.get('license') or not p.get('usage')
                    or (purpose == 'publish' and p.get('publicationApproved') is not True)):
                continue
            try:
                start, end, duration = float(p['start']), float(p['end']), float(info['duration'])
                if not all(math.isfinite(n) for n in (start, end, duration)) or not 0 <= start < end <= duration + .000001:
                    continue
            except (TypeError, ValueError, KeyError):
                continue
            source = _video_source(old, row['asset_id'], row['extension'])
            if source is None:
                continue
            path, digest = source
            if path.suffix.lower() in ('.webm', '.ogv'):
                actual = _probe_video_source(path)
                # The legacy MP4 preview's container may round the source tail
                # by part of one frame. Clamp that tail inside both the approved
                # interval and the actual original; never extend either one.
                if end > actual['duration'] + 1 / actual['fps'] + .02 or start >= actual['duration']:
                    continue
                end = min(end, actual['duration'])
                duration = actual['duration']
            rows.append({'asset_id': row['asset_id'], 'segment_id': row['segment_id'], 'name': row['name'],
                         'path': str(path), 'start': start, 'end': min(end, start + 180), 'source_duration': duration,
                         'file_sha256': digest,
                         'reviewer': row['reviewer'], 'updated': row['updated'], 'payload': p})
    return rows


def export_approved_videos(project_root, *, root=None, purpose='internal', asset_ids=None):
    """Materialize approved intervals into project output; return rail clip records.

    A content hash checks original bytes against the old library identity. Partial
    approvals are trimmed, so downstream matching cannot use an unreviewed tail.
    Existing valid exports are reused; disabled/withdrawn approval is not returned.
    """
    from reference_video import probe, tool
    project = Path(project_root).resolve()
    records = []
    for item in approved_videos(project, root=root, purpose=purpose, asset_ids=asset_ids):
        source = Path(item['path'])
        if _content_hash(source) != item['asset_id']:
            raise LegacyError('旧素材原文件已变化，请先重新审核')
        actual_source = _probe_video_source(source)
        if item['end'] > actual_source['duration'] + .000001:
            raise LegacyError('旧库审核片段超出实际视频范围')
        signature = hashlib.sha256(json.dumps(item, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]
        cid = 'legacy_' + item['asset_id'][:16] + '_' + str(item['segment_id']) + '_' + signature
        target_dir = project / 'output/reference-legacy' / cid
        target_dir.mkdir(parents=True, exist_ok=True)
        # MP4/MOV can retain their bytes only for a full approved interval.
        # WebM/OGV always becomes MP4 for the downstream editing inventory.
        whole = item['start'] == 0 and abs(item['end'] - item['source_duration']) < .000001
        copy_whole = whole and source.suffix.lower() not in ('.webm', '.ogv')
        suffix = source.suffix if copy_whole else '.mp4'
        target = target_dir / ('source' + suffix)
        if not target.is_file():
            with tempfile.TemporaryDirectory(prefix='.prepare-', dir=target_dir) as temp:
                temporary = Path(temp) / ('source' + suffix)
                if copy_whole:
                    shutil.copyfile(source, temporary)
                else:
                    process = subprocess.run([tool('ffmpeg'), '-nostdin', '-v', 'error', '-y', '-ss', str(item['start']),
                        '-protocol_whitelist', 'file', '-f', _VIDEO_DEMUXERS[source.suffix.lower()],
                        '-i', str(source), '-t', str(item['end'] - item['start']), '-map', '0:v:0', '-an',
                        '-c:v', 'libx264', '-preset', 'fast', '-pix_fmt', 'yuv420p',
                        '-vf', 'pad=ceil(iw/2)*2:ceil(ih/2)*2', '-movflags', '+faststart', str(temporary)],
                        capture_output=True, timeout=300)
                    if process.returncode:
                        raise LegacyError('旧库已审片段导出失败')
                technical = probe(temporary)
                if technical['duration'] > item['end'] - item['start'] + 1 / technical['fps'] + .02:
                    raise LegacyError('导出片段超出已审核范围')
                os.replace(temporary, target)
        technical = probe(target)
        p = item['payload']
        record = {'id': cid, 'file': target.relative_to(project).as_posix(), 'title': item['name'],
                  'tags': p.get('tags', []), 'places': [p['location']] if p.get('location') and p.get('locationEvidence') else [],
                  'quality': 3, 'issues': [p['riskNotes']] if p.get('riskNotes') else [],
                  'description': p.get('description', ''), 'clip_s': technical['duration'],
                  'pexels': p.get('originalUrl', ''), 'source_provider': '旧镜头库（来源见授权记录）',
                  'approval': {'actor': item['reviewer'], 'at': item['updated'], 'license': p['license'],
                               'usage': p['usage'], 'purpose': purpose},
                  'legacy_asset_id': item['asset_id'], 'legacy_segment_id': item['segment_id'],
                  'source_interval': [item['start'], item['end']]}
        (target_dir / 'provenance.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        records.append(record)
    return records
