"""Explicit, bounded video discovery. GETs read local data; only collect calls providers."""
import json
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, HTTPRedirectHandler, build_opener

import llm

PLATFORMS = {'YouTube': 'YOUTUBE_API_KEY', 'TikTok': 'VIDEO_TRENDS_TIKTOK_FEED_URL',
             'Instagram': 'VIDEO_TRENDS_INSTAGRAM_FEED_URL'}
LOCK = threading.Lock()
MAX_BYTES = 2_000_000
RETENTION_DAYS = 30


class FeedError(ValueError):
    pass


class BusyError(FeedError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a feed's bearer token to a redirect destination.
        return None


def config():
    env = llm.load_env()
    keys = list(PLATFORMS.values()) + ['VIDEO_TRENDS_TIKTOK_TOKEN', 'VIDEO_TRENDS_INSTAGRAM_TOKEN']
    return {key: os.environ.get(key) or env.get(key, '') for key in keys}


def date(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def request_json(url, headers=None):
    try:
        request = Request(url, headers={'Accept': 'application/json', **(headers or {})})
        with build_opener(NoRedirect).open(request, timeout=15) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise FeedError('响应超过 2 MB，请减少接口返回条数。')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise FeedError('数据格式错误：接口应返回 JSON 对象。')
        return data
    except HTTPError as e:
        # Never return provider response bodies, URLs or secrets to the browser.
        messages = {401: '凭据无效或已过期。', 403: '接口权限不足或额度已用完，请检查服务商控制台。',
                    429: '请求过于频繁或额度已用完，请稍后重试。'}
        raise FeedError(messages.get(e.code, '数据服务返回 HTTP %s，请检查接口配置。' % e.code)) from None
    except FeedError:
        raise
    except (OSError, ValueError):
        raise FeedError('无法读取数据服务，请检查网络、接口地址和 JSON 格式。') from None


def canonical(platform, value):
    try:
        p = urlsplit(str(value))
        host = (p.hostname or '').lower().removeprefix('www.').removeprefix('m.')
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            return None
        if platform == 'YouTube':
            ident = (parse_qs(p.query).get('v') or [''])[0] if host == 'youtube.com' and p.path == '/watch' else ''
            if host == 'youtu.be':
                ident = p.path.strip('/')
            if host == 'youtube.com' and p.path.startswith('/shorts/'):
                ident = p.path[len('/shorts/'):].strip('/')
            if re.fullmatch(r'[\w-]{11}', ident, flags=re.ASCII):
                return ident, 'https://www.youtube.com/watch?v=' + ident
        if platform == 'TikTok' and host == 'tiktok.com':
            match = re.fullmatch(r'/@([\w.-]+)/video/(\d+)/?', p.path, flags=re.ASCII)
            if match:
                return match[2], 'https://www.tiktok.com/@%s/video/%s' % (match[1], match[2])
        if platform == 'Instagram' and host == 'instagram.com':
            match = re.fullmatch(r'/reels?/([\w-]+)/?', p.path, flags=re.ASCII)
            if match:
                return match[1], 'https://www.instagram.com/reel/%s/' % match[1]
    except (ValueError, TypeError):
        pass
    return None


def count(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool) or not re.fullmatch(r'\d{1,18}', str(value)):
        raise FeedError('指标必须是非负整数或 null。')
    return int(value)


def normalize(platform, item, now, provider):
    if not isinstance(item, dict):
        raise FeedError('视频记录必须是 JSON 对象。')
    link = canonical(platform, item.get('url'))
    title = item.get('title')
    if not link or not isinstance(title, str) or not title.strip():
        raise FeedError('视频缺少标题或原视频链接与平台不匹配。')
    published = date(item.get('published_at'))
    measured = date(item.get('metrics_at')) or now
    if item.get('published_at') and not published:
        raise FeedError('发布时间必须为包含时区的 ISO 8601 时间。')
    if item.get('metrics_at') and not date(item.get('metrics_at')):
        raise FeedError('指标时间必须为包含时区的 ISO 8601 时间。')
    if (published and published > now + timedelta(minutes=5)) or measured > now + timedelta(minutes=5):
        raise FeedError('发布时间或指标时间不能在未来。')
    metrics = item.get('metrics') or {}
    if not isinstance(metrics, dict):
        raise FeedError('metrics 必须为 JSON 对象。')
    return {'id': platform.lower() + ':' + link[0], 'platform': platform, 'video_id': link[0],
            'title': title.strip()[:300], 'author': str(item.get('author') or '')[:200],
            'published_at': published.isoformat() if published else None,
            'original': link[1], 'url': link[1], 'source': provider, 'collected_at': now.isoformat(),
            'metrics_at': measured.isoformat(), 'metrics_time_basis': 'provider' if item.get('metrics_at') else 'retrieved',
            'metrics': {key: count(metrics.get(key)) for key in ('views', 'likes', 'comments', 'shares')}}


def youtube(cfg, query, region, days, now):
    def call(resource, args):
        return request_json('https://www.googleapis.com/youtube/v3/' + resource + '?' + urlencode(args),
                            {'X-Goog-Api-Key': cfg['YOUTUBE_API_KEY']})
    found = call('search', {'part': 'snippet', 'type': 'video', 'q': query, 'regionCode': region,
                           'publishedAfter': (now - timedelta(days=days)).isoformat(),
                           'order': 'viewCount', 'maxResults': 25})
    if not isinstance(found.get('items'), list):
        raise FeedError('YouTube 搜索响应缺少 items。')
    ids = []
    for item in found['items']:
        ident = item.get('id', {}).get('videoId') if isinstance(item, dict) else None
        if isinstance(ident, str) and re.fullmatch(r'[\w-]{11}', ident, flags=re.ASCII) and ident not in ids:
            ids.append(ident)
    if not ids:
        return []
    details = call('videos', {'part': 'snippet,statistics', 'id': ','.join(ids)})
    if not isinstance(details.get('items'), list):
        raise FeedError('YouTube 统计响应缺少 items。')
    rows = []
    for item in details['items']:
        if not isinstance(item, dict) or item.get('id') not in ids:
            continue
        s, m = item.get('snippet') or {}, item.get('statistics') or {}
        rows.append(normalize('YouTube', {'title': s.get('title'), 'author': s.get('channelTitle'),
                    'url': 'https://www.youtube.com/watch?v=' + item['id'], 'published_at': s.get('publishedAt'),
                    'metrics': {'views': m.get('viewCount'), 'likes': m.get('likeCount'), 'comments': m.get('commentCount')}},
                    now, 'YouTube Data API v3'))
    return rows


def partner_feed(platform, cfg, query, region, days, now):
    url = cfg[PLATFORMS[platform]]
    try:
        p = urlsplit(url)
        if p.scheme != 'https' or not p.hostname or p.username or p.password or p.fragment:
            raise ValueError()
        params = parse_qs(p.query)
        params.update(q=[query], region=[region], days=[str(days)], limit=['25'])
        url = p._replace(query=urlencode(params, doseq=True)).geturl()
    except ValueError:
        raise FeedError('数据服务地址必须为 HTTPS，不能含账号密码或片段。') from None
    token = cfg.get('VIDEO_TRENDS_' + platform.upper() + '_TOKEN')
    data = request_json(url, {'Authorization': 'Bearer ' + token} if token else {})
    if not isinstance(data.get('items'), list) or len(data['items']) > 100:
        raise FeedError('数据服务须返回 items 数组，最多 100 条。请按接入说明适配字段。')
    return [normalize(platform, item, now, platform + ' 数据服务') for item in data['items']]


def load(out, now):
    path = Path(out) / 'video-trends.json'
    if not path.exists():
        return {'version': 1, 'items': [], 'runs': {}, 'last_request': None}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if data.get('version') != 1 or not isinstance(data.get('items'), list) or not isinstance(data.get('runs'), dict):
            raise ValueError()
        # Public API data is a rolling cache, never indefinite archival.
        cutoff = now - timedelta(days=RETENTION_DAYS)
        data['items'] = [row for row in data['items'] if (date(row.get('collected_at')) or cutoff) > cutoff]
        return data
    except (OSError, ValueError, TypeError, AttributeError):
        raise FeedError('本地视频数据文件无法读取，请保留文件并检查后重试。') from None


def save(out, data):
    Path(out).mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.video-trends-', dir=out)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, allow_nan=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, Path(out) / 'video-trends.json')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def present(data, cfg, now):
    platforms = []
    for name, key in PLATFORMS.items():
        run = data['runs'].get(name, {})
        status = '缺少 API Key' if name == 'YouTube' else '缺少数据服务地址'
        if cfg.get(key):
            status = '已配置 · 尚未采集'
            if run.get('error'):
                status = '最近采集失败'
            elif run.get('succeeded_at'):
                status = '最近采集成功 · %s 条' % run['count']
        platforms.append({'name': name, 'configured': bool(cfg.get(key)), 'status': status, **run})
    rows = sorted(data['items'], key=lambda r: (r['metrics']['views'] is not None, r['metrics']['views'] or 0), reverse=True)
    return {'items': rows, 'platforms': platforms, 'last_request': data.get('last_request'), 'checked_at': now.isoformat()}


def read(out):
    now = datetime.now(timezone.utc)
    with LOCK:
        data = load(out, now)
        # Persist cache expiry even if the user only reads, without any remote calls.
        if (Path(out) / 'video-trends.json').exists():
            save(out, data)
    return present(data, config(), now)


def collect(out, body, now=None):
    if not isinstance(body, dict):
        raise FeedError('采集请求须为 JSON 对象。')
    query, region, days = body.get('query'), body.get('region', 'GB'), body.get('days', 7)
    selected = body.get('platforms', list(PLATFORMS))
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
        raise FeedError('请输入 1–100 字的搜索关键词。')
    if not isinstance(region, str) or not re.fullmatch(r'[A-Z]{2}', region):
        raise FeedError('地区须为两位大写国家代码。')
    if type(days) is not int or days not in (1, 7, 30):
        raise FeedError('时间范围仅支持 1、7 或 30 天。')
    if not isinstance(selected, list) or not selected or any(not isinstance(p, str) or p not in PLATFORMS for p in selected):
        raise FeedError('请选择支持的平台。')
    if not LOCK.acquire(blocking=False):
        raise BusyError('已有视频采集进行中，请稍后刷新。')
    try:
        now = now or datetime.now(timezone.utc)
        cfg = config()
        data = load(out, now)
        # Prevent accidental duplicate requests consuming provider quota.
        last = data.get('last_request') or {}
        if date(last.get('at')) and (now - date(last['at'])).total_seconds() < 30:
            raise BusyError('请间隔 30 秒后再次采集，刷新列表不会消耗接口额度。')
        data['last_request'] = {'query': query.strip(), 'region': region, 'days': days, 'at': now.isoformat()}
        for platform in dict.fromkeys(selected):
            previous = data['runs'].get(platform, {})
            run = {**previous, 'attempted_at': now.isoformat(), 'query': query.strip()}
            try:
                if not cfg[PLATFORMS[platform]]:
                    raise FeedError('请先在本机 .env 填写 ' + PLATFORMS[platform] + '。')
                rows = youtube(cfg, query.strip(), region, days, now) if platform == 'YouTube' else partner_feed(platform, cfg, query.strip(), region, days, now)
                by_id = {row['id']: row for row in data['items']}
                for row in rows:
                    old = by_id.get(row['id'])
                    # Out-of-order provider snapshots must not overwrite newer metrics.
                    if not old or date(row['metrics_at']) >= date(old['metrics_at']):
                        by_id[row['id']] = row
                data['items'] = sorted(by_id.values(), key=lambda r: r['collected_at'], reverse=True)[:500]
                run.update(succeeded_at=now.isoformat(), count=len({r['id'] for r in rows}), error=None)
            except FeedError as e:
                run['error'] = str(e)
            except (ValueError, TypeError, AttributeError, KeyError):
                run['error'] = '数据服务返回的字段不符合接口格式，请按接入说明检查。'
            data['runs'][platform] = run
        save(out, data)
        return present(data, cfg, now)
    finally:
        LOCK.release()
