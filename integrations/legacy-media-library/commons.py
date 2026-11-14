"""Collect public Commons evidence and queue local files for human review."""
from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sqlite3
import time
import http_transport
import search_agent
import search_recovery
import video_sources
from shot_search import is_reference_request
import urllib.error
import urllib.parse
import urllib.request

API = 'https://commons.wikimedia.org/w/api.php'
HOSTS = {'commons.wikimedia.org', 'upload.wikimedia.org'}
MAX_FILE = 12 * 1024 * 1024
MAX_VIDEO = 50 * 1024 * 1024
MAX_CANDIDATES = 6
MAX_AGENT_CANDIDATES = 20
MAX_AGENT_DOWNLOADS = 5
COMMONS_LOCATIONS = {
    "King's Cross": ('king’s cross', "king's cross", 'kings cross', '国王十字'),
}
MIME_EXT = {'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp',
            'video/mp4':'.mp4','video/quicktime':'.mov','video/webm':'.webm'}

SCHEMA = """
CREATE TABLE IF NOT EXISTS commons_job(
 id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
 result TEXT NOT NULL DEFAULT '{}', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS commons_candidate(
 page_id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL, payload TEXT NOT NULL,
 asset TEXT, note TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'hold');
"""

class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def check_url(url):
    p = urllib.parse.urlsplit(url)
    if p.scheme != 'https' or p.netloc not in HOSTS:
        raise ValueError('来源链接超出 Wikimedia 白名单')
    return url

def fetch(url, limit, mime=None, timeout=15):
    return http_transport.read(url, limit, headers={
        'User-Agent':'AgentRailMediaLibrary/1.0 (local Wikimedia Commons media review; Python urllib)'
    }, mime=mime, timeout=timeout, validate_url=check_url, follow_redirects=True)

class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_data(self, value):
        self.parts.append(value)

def plain(value, limit=2000):
    parser = PlainText()
    parser.feed(str(value or '')[:30000])
    return ' '.join(' '.join(parser.parts).split())[:limit]

def page_key(url):
    p = urllib.parse.urlsplit(url)
    return urllib.parse.unquote(p.path).replace('_',' ').casefold() if p.netloc=='commons.wikimedia.org' else ''

def collect(query, timeout=15):
    url = API+'?'+urllib.parse.urlencode(dict(
        action='query',format='json',formatversion=2,generator='search',
        gsrnamespace=6,gsrsearch=query,gsrlimit=4,prop='imageinfo',
        iiprop='url|mime|size|extmetadata|user',iiextmetadatalanguage='en',maxlag=5))
    response = json.loads(fetch(url,2*1024*1024,timeout=timeout))
    if response.get('error',{}).get('code') in ('maxlag','ratelimited'):
        raise urllib.error.HTTPError(url,429,'Commons 限流',None,None)
    if 'error' in response:
        raise ValueError('Commons 查询失败：'+plain(response['error'].get('info'),200))
    return sorted(response.get('query',{}).get('pages',[]),key=lambda p:p.get('index',999))

def evidence(page):
    info = page['imageinfo'][0]
    metadata = info.get('extmetadata',{})
    def field(key):
        return plain(metadata.get(key,{}).get('value',''))
    title = page['title'].removeprefix('File:')
    found = check_url(info.get('descriptionurl') or 'https://commons.wikimedia.org/wiki/'+urllib.parse.quote(page['title']))
    return dict(title=title, discovery=found, originalClaim=field('Source') or field('Credit'),
                author=field('Artist'), uploader=plain(info.get('user')), license=field('LicenseShortName'),
                licenseUrl=field('LicenseUrl'), restrictions=field('Restrictions'),
                attribution=field('Attribution'), description=field('ImageDescription'),
                mime=info.get('mime',''), size=info.get('size',0), mediaUrl=info.get('url',''),
                width=info.get('width',0), height=info.get('height',0), duration=info.get('duration'),
                fetchedAt=datetime.now(timezone.utc).isoformat(timespec='seconds'))

def public_license(value):
    return bool(re.fullmatch(r'CC (?:BY(?:-SA)?|0)(?: \d\.\d)?|Public domain',value,re.I))

def video_mime(info):
    return info['mime'].startswith('video/') or (
        info['mime']=='application/ogg' and info['title'].lower().endswith('.ogv'))


def file_limit(info):
    return MAX_VIDEO if video_mime(info) else MAX_FILE


def store_lead(conn, job_id, key, info, note):
    return conn.execute(
        'INSERT OR IGNORE INTO commons_candidate(page_id,job_id,payload,note) VALUES (?,?,?,?)',
        (key,job_id,json.dumps(info,ensure_ascii=False),note)).rowcount


def record_candidate(counts, key, outcome):
    counts[outcome] += 1
    counts['pageIds'].append(key)
    counts['seen'].append(key)
    counts['found'] = len(counts['seen'])


def queue_candidate(conn, media, job_id, page, query, timeout=15, *, hold_reason='', stop_on_limit=False):
    page_id = page['pageid']
    row = conn.execute('SELECT asset,note,state FROM commons_candidate WHERE page_id=?',(page_id,)).fetchone()
    info = evidence(page)
    legacy_block = row and not row['asset'] and row['state']=='hold' and video_mime(info) and (
        row['note'].startswith(('本地处理流程不支持','文件超过自动采集的 12 MB','下载失败：')))
    if row and (not legacy_block or hold_reason):
        return 'duplicate'
    info['query'] = query
    if hold_reason:
        status, key, reason = 'lead', None, hold_reason
    else:
        retry = (page_id,row['note']) if row else None
        alternate = preferred_mp4(page_id,info,min(2,timeout/3))
        if alternate:
            status,key,reason = materialize(conn,media,alternate,retry,timeout,stop_on_limit=stop_on_limit)
        else:
            key = None
        if key:
            info,reason = alternate,'已使用 Commons 同页 MP4 版本'
        elif conn.in_transaction:
            return 'duplicate'
        else:
            status,key,reason = materialize(conn,media,info,retry,timeout,stop_on_limit=stop_on_limit)
    if row:
        if key or reason:
            conn.execute("UPDATE commons_candidate SET payload=?,asset=?,note=? WHERE page_id=? AND asset IS NULL AND state='hold' AND note=?",
                         (json.dumps(info,ensure_ascii=False),key,reason,page_id,row['note']))
        return status
    conn.execute('INSERT INTO commons_candidate(page_id,job_id,payload,asset,note) VALUES (?,?,?,?,?)',
                 (page_id,job_id,json.dumps(info,ensure_ascii=False),key,reason))
    return status

def materialize(conn, media, info, retry=None, timeout=15, *, stop_on_limit=False):
    extension = MIME_EXT.get(info['mime'])
    limit = file_limit(info)
    known = conn.execute("""SELECT s.asset,s.payload FROM segment s
                             WHERE s.state!='rejected'""").fetchall()
    discovery_key = page_key(info['discovery'])
    key = next((r['asset'] for r in known if discovery_key and
                page_key(json.loads(r['payload']).get('originalUrl',''))==discovery_key),None)
    status, reason = 'lead', ''
    if key:
        status, reason = 'duplicate', '已有关联素材，保留原审核记录。'
    elif not public_license(info['license']) or not info['author'] or (
            info['license'].upper().startswith('CC') and not info['licenseUrl']):
        reason = '网页作者或开放许可证据不足，需人工核实。'
    elif not extension:
        reason = '本地处理流程不支持该媒体格式，请核对来源或关联可用文件。'
    elif info['size'] is not None and not 0 < info['size'] <= limit:
        reason = f'文件超过自动采集的 {limit//(1024*1024)} MB 上限，保留网页线索。'
    else:
        try:
            download = video_sources.archive_read if info.get('provider')=='Internet Archive' else fetch
            if stop_on_limit:
                data = search_agent.within(timeout,download,info['mediaUrl'],limit,info['mime'],timeout)
            else:
                data = download(info['mediaUrl'],limit,info['mime'],timeout)
            if info['size'] is not None and len(data) != info['size']:
                raise ValueError('文件大小与来源记录不同')
            key = sha256(data).hexdigest()
        except (OSError,ValueError) as exc:
            key = None
            if stop_on_limit and search_recovery.classification(exc):
                raise
            reason = '下载失败：'+str(exc)[:180]
        if key:
            conn.execute('BEGIN IMMEDIATE')
            if retry and not conn.execute(
                "SELECT 1 FROM commons_candidate WHERE page_id=? AND asset IS NULL AND state='hold' AND note=?",
                retry,
            ).fetchone():
                return 'duplicate', None, ''
            if conn.execute('SELECT 1 FROM asset WHERE id=?',(key,)).fetchone():
                status, reason = 'duplicate', '文件内容已存在，保留原审核记录。'
            else:
                folder = media / key
                folder.mkdir(exist_ok=True)
                (folder / ('source.webm' if extension=='.webm' else 'original'+extension)).write_bytes(data)
                conn.execute("INSERT INTO asset(id,name,extension,status) VALUES (?,?,?,'queued')",
                             (key,Path(info['title']).name,'.mp4' if extension=='.webm' else extension))
                conn.execute('INSERT INTO source(asset,name,uploader,origin) VALUES (?,?,?,?)',
                             (key,info['title'],info.get('provider','Wikimedia Commons'),info['discovery']))
                if extension=='.webm':
                    reason = '已保存原始 WebM，后台生成 MP4 预览。'
                status = 'downloaded'
    return status, key, reason

def retry_payload(conn, page_id):
    row = conn.execute("""SELECT c.*,j.payload job FROM commons_candidate c
                          JOIN commons_job j ON j.id=c.job_id WHERE c.page_id=?""",(page_id,)).fetchone()
    if not row or row['asset'] or row['state'] != 'hold' or not row['note'].startswith('下载失败：'):
        raise ValueError('只有未关联文件的下载失败项可以重试')
    running = conn.execute("SELECT payload FROM commons_job WHERE state IN ('queued','running')")
    if any(json.loads(j['payload']).get('retryPage')==row['page_id'] for j in running):
        raise ValueError('此候选已在重试')
    return {**json.loads(row['job']), 'retryPage':row['page_id'], 'queries':[]}

def run_retry(conn, media, page_id):
    row = conn.execute("SELECT * FROM commons_candidate WHERE page_id=? AND asset IS NULL AND state='hold'",(page_id,)).fetchone()
    if not row or not row['note'].startswith('下载失败：'):
        return 'duplicate'
    status, key, reason = materialize(conn,media,json.loads(row['payload']),(page_id,row['note']))
    if key or reason:
        conn.execute("UPDATE commons_candidate SET asset=?,note=? WHERE page_id=? AND asset IS NULL AND state='hold' AND note=?",
                     (key,reason,page_id,row['note']))
    return status

def save_job(conn, job_id, counts, state='running'):
    conn.execute('UPDATE commons_job SET state=?,result=? WHERE id=?',
                 (state,json.dumps(counts,ensure_ascii=False),job_id))

def agent_candidates(conn, page_ids):
    summaries = []
    for page_id in page_ids:
        row = conn.execute('SELECT * FROM commons_candidate WHERE page_id=?',(page_id,)).fetchone()
        if not row:
            continue
        info = json.loads(row['payload'])
        summaries.append(dict(pageId=page_id,title=info['title'][:180],description=info['description'][:350],
                              mime=info['mime'],size=info['size'],license=info['license'],
                              sourceKnown=bool(info['author'] and info['discovery']),
                              localFile=bool(row['asset']),note=row['note'][:160],
                              versionEligible=not row['asset'] and row['state']=='hold' and info['mime'].startswith('video/')))
    return summaries

def video_versions(page_id,wait):
    url = API+'?'+urllib.parse.urlencode(dict(action='query',format='json',formatversion=2,
                pageids=page_id,prop='videoinfo',viprop='derivatives',maxlag=5))
    response = json.loads(search_agent.within(wait,fetch,url,2*1024*1024,None,wait))
    if response.get('error',{}).get('code') in ('maxlag','ratelimited'):
        raise urllib.error.HTTPError(url,429,'Commons 限流',None,None)
    if 'error' in response:
        raise ValueError('Commons 无法读取视频版本')
    pages = response.get('query',{}).get('pages',[])
    return next((v['derivatives'] for p in pages for v in p.get('videoinfo',[]) if 'derivatives' in v),[])


def preferred_mp4(page_id,info,wait):
    if info['mime']!='video/webm' or not public_license(info['license']) or not info['author']:
        return None
    try:
        choices = [version for version in video_versions(page_id,wait) if version.get('src')
                   and version.get('type','').split(';')[0]=='video/mp4'
                   and type(version.get('size')) is int and 0 < version['size'] <= MAX_VIDEO]
        if choices:
            version = max(choices,key=lambda entry:int(entry.get('height') or 0))
            return {**info,'originalMediaUrl':info['mediaUrl'],
                    'mediaUrl':check_url(urllib.parse.urljoin(API,version['src'])),
                    'mime':'video/mp4','size':version['size']}
    except (OSError,ValueError,KeyError,TypeError) as exc:
        if isinstance(exc,urllib.error.HTTPError) and exc.code in (429,503):
            raise
    return None


def check_version(conn, media, page_id, timeout):
    row = conn.execute("SELECT * FROM commons_candidate WHERE page_id=? AND asset IS NULL AND state='hold'",(page_id,)).fetchone()
    if not row:
        return '候选已处理'
    info = json.loads(row['payload'])
    versions = video_versions(page_id,timeout())
    for version in versions:
        mime = version.get('type','').split(';')[0]
        if mime not in ('video/mp4','video/quicktime') or not version.get('src'):
            continue
        candidate = {**info,'originalMediaUrl':info['mediaUrl'],'mediaUrl':check_url(urllib.parse.urljoin(API,version['src'])),
                     'mime':mime,'size':None}
        status,key,reason = materialize(conn,media,candidate,(page_id,row['note']),timeout(),stop_on_limit=True)
        if not key:
            return reason or '候选已处理'
        changed = conn.execute(
            "UPDATE commons_candidate SET asset=?,payload=?,note=? WHERE page_id=? AND asset IS NULL AND state='hold' AND note=?",
            (key,json.dumps(candidate,ensure_ascii=False),'已关联 Commons 同页的视频版本',page_id,row['note'])
        ).rowcount
        if not changed:
            return '候选已由审核人处理'
        return '已关联可处理版本' if status=='downloaded' else '已关联库中已有文件'
    return '没有可由本地处理的 MP4 或 MOV 版本，保留原线索'

def evidence_score(info, body):
    from shot_search import CONCEPTS, LINES, PLACES, PARENTS, contains, detect, normalize
    text = normalize(info['title']+' '+info['description'])
    places = detect(text,PLACES)
    places |= set().union(*(PARENTS.get(place,set()) for place in places))
    observed = places | detect(text,LINES) | detect(text,CONCEPTS)
    width,height = info.get('width',0),info.get('height',0)
    if width and height:
        observed.add('横屏' if width>height else '竖屏' if width<height else '方形')
    rules = body['rules'][1:]
    hits = sum(rule in observed or (
        rule not in {'横屏','竖屏','方形'} and not rule.startswith('视频至少') and contains(text,rule)
    ) for rule in rules)
    return hits, len(rules)

def assess(info, body, duration=None):
    from shot_search import CONCEPTS, LINES, PLACES, PARENTS, contains, detect, normalize, explicit_names
    search = body.get('search',{})
    required = body['rules'][1:]
    title = normalize(info['title'])
    description = normalize(info['description'])
    # A street name is not evidence that the photo shows the station.
    title = re.sub(r'\beuston (?:road|square)\b','',title)
    description = re.sub(r'\beuston (?:road|square)\b','',description)
    text = title+' '+description
    places = {**PLACES,**COMMONS_LOCATIONS}
    required_places = set(required) & places.keys()
    if search.get('location'):
        required_places |= explicit_names(search['location'],places)
    named = detect(title,places)
    observed_places = detect(text,places)
    observed_places |= set().union(*(PARENTS.get(place,set()) for place in observed_places))
    missing_places = sorted(p for p in required_places if p not in observed_places and not (
        p not in places and contains(text,p)))
    conflicts = named - required_places - set().union(*(PARENTS.get(p,set()) for p in required_places))
    conflicts = {p for p in conflicts if not PARENTS.get(p,set()) & required_places}
    if required_places and re.search(r'\b(?:inaccurate depiction|fictional recreation)\b',description):
        return '来源说明明确指出画面为虚构或不准确的再现',[],[]
    broad_places = set().union(*PARENTS.values()) | {'瑞士'}
    different_named_place = bool((conflicts-broad_places) and (required_places-broad_places)
                                 and not named & required_places)
    if conflicts and (missing_places or different_named_place):
        return '地点不符：标题指向'+ '、'.join(sorted(conflicts)),[],[]
    gaps = ['缺少地点线索：'+p for p in missing_places]
    line = search.get('line','')
    lines = (set(required) & LINES.keys()) | explicit_names(line,LINES)
    gaps.extend('缺少线路线索：'+item for item in sorted(lines)
                if item not in detect(text,LINES) and not contains(text,item))
    seconds = float(search.get('seconds') or 0)
    mime = info['mime']
    video = video_mime(info)
    if not video and not mime.startswith('image/'):
        return '媒体类型不符：需要图片或视频',[],[]
    if body.get('videoOnly') and not video:
        return '媒体类型不符：需要视频',[],[]
    if duration is None:
        duration = info.get('duration')
    try:
        duration = float(duration)
    except (ValueError,TypeError):
        duration = None
    if seconds and duration is not None and 0 < duration < seconds:
        return f'时长不符：{duration:g} 秒，要求至少 {seconds:g} 秒',[],[]
    width,height = info.get('width',0),info.get('height',0)
    orientations = {'横屏','竖屏','方形'}
    orientation = next((rule for rule in required if rule in orientations),'')
    if orientation and width and height:
        actual = '横屏' if width>height else '竖屏' if width<height else '方形'
        if actual != orientation:
            return f'画幅不符：{actual}，要求{orientation}',[],[]
    concepts = detect(text,CONCEPTS)
    features = [*required, *sorted(detect(normalize(body['script']),CONCEPTS)-set(required))]
    for feature in features:
        if feature in required_places or feature in lines or feature in orientations or feature.startswith('视频至少'):
            continue
        synonyms = detect(normalize(feature),CONCEPTS)
        if feature not in concepts and not (synonyms & concepts) and not contains(text,feature):
            gaps.append('缺少画面线索：'+feature)
    checks = []
    if seconds and not duration:
        checks.append(f'核验视频是否至少 {seconds:g} 秒')
    if orientation and not (width and height):
        checks.append('核验画幅')
    if not public_license(info['license']) or not info['author'] or (
            info['license'].upper().startswith('CC') and not info['licenseUrl']):
        checks.append('核验作者和授权来源')
    if is_reference_request(body):
        # Absence from a webpage is not evidence of a visual mismatch. A short,
        # licensed video may be downloaded for frame/interval review without
        # proving its camera movement in the title. Explicit conflicts above
        # still reject it, and missing authorization still blocks materialize.
        checks.extend('待画面或来源核验：'+gap for gap in gaps)
        checks.append('完整分镜的主体、动作、景别、方向、构图和连续区间待实片核验')
        gaps = []
    return '',gaps,checks

def candidate_gaps(conn, body, page_ids):
    usable = []
    for page_id in page_ids:
        row = conn.execute("""SELECT c.payload,c.state,c.asset,a.info,a.status FROM commons_candidate c
                              LEFT JOIN asset a ON a.id=c.asset WHERE c.page_id=?""",(page_id,)).fetchone()
        if not row or row['state']=='rejected':
            continue
        info = json.loads(row['payload'])
        duration = json.loads(row['info'] or '{}').get('duration')
        exclusion,gaps,checks = assess(info,body,duration)
        if not exclusion:
            local = bool(row['asset']) and row['status']!='failed'
            download_blocked = (info['mime'] not in MIME_EXT
                                or (info['size'] is not None and not 0 < info['size'] <= file_limit(info)))
            if body.get('videoOnly') and not local and (
                    download_blocked or '核验作者和授权来源' not in checks):
                gaps = [*gaps,'仍缺可获取的视频文件']
            if is_reference_request(body):
                # Metadata and download status never certify the reference shot.
                gaps = [*gaps,'尚无按完整分镜画面核验通过的候选；可根据网页线索调整检索词']
            usable.append((gaps,checks,local))
    if not usable:
        seconds = float(body.get('search',{}).get('seconds') or 0)
        gaps = [f"仍缺至少 {seconds:g} 秒的视频" if seconds else "仍缺匹配的视频"] if body.get('videoOnly') else []
        return ['没有符合本次条件的候选',*gaps],[]
    gaps,checks,_ = min(usable,key=lambda item:(len(item[0]),not item[2],len(item[1])))
    return gaps,checks

def commons_cap(body):
    if body.get('mode')=='agent':
        return MAX_AGENT_CANDIDATES-4 if body.get('videoOnly') and body.get('supplementSources') else MAX_AGENT_CANDIDATES
    return MAX_CANDIDATES-2 if body.get('videoOnly') and body.get('supplementSources') else MAX_CANDIDATES


def commons_seen(counts):
    # Archive/web leads have negative IDs. They must not consume the Commons
    # quota when a failed Commons round resumes with supplemental leads present.
    return sum(type(key) is int and key > 0 for key in counts.get('seen', []))


def run_agent_job(db, media, job_id, body, counts):
    try:
        config = search_agent.settings()
    except search_agent.Unavailable:
        config = None
    rounds = counts.setdefault('rounds',[])
    excluded = counts.setdefault('excluded',[])
    if counts.get('resume_target'):
        # Time in the queue is not source execution time. A resumed job may
        # wait behind four tasks or a provider cooldown before being claimed.
        counts['startedAt'] = time.time()
        counts.pop('supplementStartedAt',None)
    started = counts.setdefault('startedAt',time.time())

    def remaining():
        end = started+search_agent.TASK_SECONDS
        if 'supplementStartedAt' in counts:
            end = min(end,counts['supplementStartedAt']+search_agent.SUPPLEMENT_SECONDS)
        return end-time.time()

    def timeout():
        left = remaining()
        if left<=0:
            raise TimeoutError('已达到搜索时限')
        return min(12,left)

    while not counts.get('stopReason') and remaining()>0:
        action = rounds[-1] if rounds and not rounds[-1].get('complete') else None
        if action is None:
            if not rounds:
                action = {'action':'search','query':body['queries'][0],'reason':'固定搜索首轮'}
            else:
                with closing(db()) as conn:
                    gaps,checks = candidate_gaps(conn,body,counts['pageIds'])
                    candidates = agent_candidates(conn,counts['pageIds'])
                counts['remainingGaps'],counts['verification'] = gaps,checks
                if not gaps:
                    counts['stopReason'] = '已有符合条件的网页线索，等待人工核验'
                    break
                searches = [r for r in rounds if r['action']=='search']
                if len(searches)>search_agent.SUPPLEMENT_LIMIT or commons_seen(counts)>=commons_cap(body):
                    counts['stopReason'] = '已达到补搜轮次或候选上限'
                    break
                counts.setdefault('supplementStartedAt',time.time())
                with closing(db()) as conn, conn:
                    save_job(conn,job_id,counts)
                version = next((c for c in candidates if c['versionEligible'] and c['sourceKnown']
                    and public_license(c['license']) and c['mime'] not in MIME_EXT
                    and c['note'].startswith('本地处理流程不支持')),None)
                if version and not counts.get('versionChecked'):
                    action = {'action':'version','pageId':version['pageId'],'reason':'检查同页可处理的视频版本'}
                    counts['versionChecked'] = True
                else:
                    action = search_agent.next_action(config,body,rounds,candidates,gaps,remaining())
                    counts.setdefault('planning',[]).append({
                        'after_search_round':len(searches), 'action':action['action'],
                        'query':action.get('query',''), 'reason':action.get('reason',''),
                        'called':action.get('planner_called',False), 'status':action.get('planner_status','unknown')})
                if action['action']=='stop':
                    counts['stopReason'] = action['reason']
                    break
            action['pageIds'] = []
            rounds.append(action)
            with closing(db()) as conn, conn:
                save_job(conn,job_id,counts)
        try:
            if action['action']=='version':
                with closing(db()) as conn, conn:
                    action['result'] = check_version(conn,media,action['pageId'],timeout)
            else:
                wait = timeout()
                pages = search_agent.within(wait,collect,action['query'],wait)
                screened = []
                for page in pages:
                    page_id = page.get('pageid')
                    if not page_id or page_id in counts['seen'] or not page.get('imageinfo'):
                        continue
                    if any(item['pageId']==page_id for item in excluded):
                        continue
                    info = evidence(page)
                    reason,gaps,checks = assess(info,body)
                    if reason:
                        excluded.append({'pageId':page_id,'title':info['title'],'reason':reason})
                    else:
                        screened.append((page,gaps,checks))
                for page,gaps,checks in sorted(screened,key=lambda item:(len(item[1]),len(item[2]))):
                    if commons_seen(counts)>=commons_cap(body) or remaining()<=0:
                        break
                    page_id = page['pageid']
                    with closing(db()) as conn, conn:
                        hold_reason = ('；'.join(gaps+checks) if gaps or '核验作者和授权来源' in checks else '')
                        if counts['downloaded']>=MAX_AGENT_DOWNLOADS and not hold_reason:
                            hold_reason = '本任务自动下载额度已用完，保留候选供后续核验'
                        outcome = queue_candidate(conn,media,job_id,page,action['query'],timeout(),
                            hold_reason=hold_reason,
                            stop_on_limit=True)
                        counts[outcome] += 1
                        counts['seen'].append(page_id)
                        counts['pageIds'].append(page_id)
                        action['pageIds'].append(page_id)
                        counts['found'] = len(counts['seen'])
                        save_job(conn,job_id,counts)
                action['result'] = f"返回 {len(pages)} 条，本轮记录 {len(action['pageIds'])} 条"
        except (OSError,ValueError,KeyError,TypeError,IndexError,sqlite3.Error) as exc:
            failure = search_recovery.record_failure(counts,exc,target={
                'stage':'commons','round_index':len(rounds)-1})
            if isinstance(exc,urllib.error.HTTPError) and exc.code in (429,503):
                counts['stopReason'] = 'Commons 暂时限流或不可用，已保留结果，可稍后重试'
            elif isinstance(exc,TimeoutError) or remaining()<=0:
                counts['stopReason'] = '已达到搜索时限，保留已有候选'
            else:
                counts['stopReason'] = 'Commons 查询或处理失败，保留已有候选'
            action['result'] = str(exc)[:200]
            counts['errors'].append(action['result'])
            action['complete'] = not bool(failure)
        else:
            action['complete'] = True
            if counts.get('resume_target',{}).get('stage') == 'commons':
                counts.pop('resume_target',None)
                counts['stopReason'] = '失败的来源查询已恢复，保留结果等待核验'
        with closing(db()) as conn, conn:
            save_job(conn,job_id,counts)
    counts.setdefault('stopReason','已达到搜索时限，保留已有候选')
    if 'supplementStartedAt' in counts:
        counts['supplementSeconds'] = round(time.time()-counts['supplementStartedAt'],2)
    finish_video_job(db,media,body,job_id,counts)
    state = ('partial' if counts['found'] else 'failed') if counts['errors'] else 'done'
    with closing(db()) as conn, conn:
        counts['remainingGaps'],counts['verification'] = candidate_gaps(conn,body,counts['pageIds'])
        save_job(conn,job_id,counts,state)

def archive_failure(counts, status, exc, *, deferred_until=None):
    transient = {'network_retries':counts.get('network_retries',0)}
    failure = search_recovery.record_failure(transient,exc,source='Internet Archive',target={'stage':'supplemental'})
    if not failure:
        return False
    if deferred_until is not None:
        failure['cooldown_until'] = deferred_until
        failure['retry_after_seconds'] = max(1,int(deferred_until-time.time()+0.999))
    status.update(failure=failure,error=str(exc)[:180],complete=False)
    if not counts.get('failure'):
        counts.update(failure=failure,cooldown_until=failure['cooldown_until'],
                      retry_after_seconds=failure['retry_after_seconds'])
    counts['errors'].append(status['error'])
    return True


def finish_video_job(db, media, body, job_id, counts):
    if not body.get('supplementSources') or not body['videoOnly'] or counts.get('supplemental',{}).get('complete'):
        return
    with closing(db()) as conn:
        gaps,_ = candidate_gaps(conn,body,counts['pageIds'])
    if not gaps and counts['pageIds']:
        return
    completed = [r for r in counts.get('rounds',[]) if r.get('action')=='search' and r.get('complete') and r.get('query')]
    query = (completed[-1]['query'] if completed else body['queries'][0]).removesuffix(' filetype:video')
    query_origin = 'latest_completed_search' if completed else 'initial_query'
    # One broader source query. Preserve all hard criteria when screening its results.
    if body.get('anchor') and not is_reference_request(body):
        from shot_search import CONCEPTS, detect, normalize
        features = detect(normalize(body['script']),CONCEPTS)
        subject = next((term for concept,term in [('湖泊','train'),('发车','train'),
                       ('铁路高架桥','viaduct')] if concept in features),'train')
        query = 'West Highland train' if body['anchor']=='scotland' and '车窗视角' in features else body['anchor']+' '+subject
        query_origin = 'legacy_anchor_query'
    status = counts.setdefault('supplemental',dict(provider='Internet Archive',query=query,returned=0,added=0))
    if counts.get('resume_target',{}).get('stage')=='supplemental' and status.get('query'):
        query = status['query']  # Resume the exact source query, not a new plan.
    else:
        status.update(query=query,query_origin=query_origin)
    with closing(db()) as conn:
        until = search_recovery.source_cooldown(conn,'Internet Archive')
    if until > time.time():
        archive_failure(counts,status,urllib.error.HTTPError('',429,'补充来源正在冷却',None,None),deferred_until=until)
        with closing(db()) as conn, conn:
            save_job(conn,job_id,counts)
        return
    try:
        hits = search_agent.within(8,video_sources.archive_search,query,8)
        status['returned'] = len(hits)
    except (OSError,ValueError,KeyError,TypeError) as exc:
        hits = []
        status['error'] = str(exc)[:180]
        if archive_failure(counts,status,exc):
            with closing(db()) as conn, conn:
                save_job(conn,job_id,counts)
            return
    else:
        status.pop('error',None)
        status.pop('failure',None)
    for key,info in hits:
        cap = MAX_AGENT_CANDIDATES if body.get('mode')=='agent' else MAX_CANDIDATES
        if key in counts['seen'] or len(counts['seen'])>=cap:
            continue
        excluded,missing,checks = assess(info,body)
        if excluded or any(item.startswith(('缺少地点','缺少线路')) for item in missing):
            continue
        note = '；'.join(['补充来源网页线索，待核对画面与下载授权',*missing,*checks])
        with closing(db()) as conn, conn:
            exists = conn.execute('SELECT 1 FROM commons_candidate WHERE page_id=?',(key,)).fetchone()
            if exists:
                record_candidate(counts,key,'duplicate')
                save_job(conn,job_id,counts)
                continue
            asset = None
            outcome = 'lead'
            if (not missing and info['author'] and video_sources.archive_license(info['licenseUrl'])
                    and (body.get('mode')!='agent' or counts['downloaded']<MAX_AGENT_DOWNLOADS)):
                try:
                    file = search_agent.within(4,video_sources.archive_file,info,MAX_VIDEO,4)
                    if file:
                        outcome,asset,note = materialize(conn,media,file,timeout=8,stop_on_limit=True)
                        info = file
                except (OSError,ValueError,TypeError,KeyError) as exc:
                    note = '准备补充来源文件失败：'+str(exc)[:150]
                    if archive_failure(counts,status,exc):
                        save_job(conn,job_id,counts)
                        return
            conn.execute('INSERT INTO commons_candidate(page_id,job_id,payload,asset,note) VALUES (?,?,?,?,?)',
                         (key,job_id,json.dumps(info,ensure_ascii=False),asset,note))
            status['added'] += 1
            record_candidate(counts,key,outcome)
            save_job(conn,job_id,counts)
    with closing(db()) as conn, conn:
        status['complete'] = True
        status.pop('failure',None)
        if counts.get('resume_target',{}).get('stage') == 'supplemental':
            counts.pop('resume_target',None)
        counts['remainingGaps'] = gaps
        save_job(conn,job_id,counts)


def run_fixed_job(db, media, job_id, body, counts):
    seen = set(counts['seen'])
    for index in range(counts['cursor'],len(body['queries'])):
        if commons_seen(counts)>=commons_cap(body):
            break
        query = body['queries'][index]
        failed = False
        try:
            pages = collect(query)
            for page in pages:
                page_id = page.get('pageid')
                if not page_id or page_id in seen or not page.get('imageinfo') or commons_seen(counts)>=commons_cap(body):
                    continue
                with closing(db()) as conn, conn:
                    hold = ''
                    if body.get('videoOnly'):
                        reason,gaps,checks = assess(evidence(page),body)
                        if reason:
                            continue
                        if gaps:
                            hold = '；'.join(gaps+checks)
                    outcome = queue_candidate(conn,media,job_id,page,query,hold_reason=hold,stop_on_limit=True)
                    counts[outcome] += 1
                    counts['pageIds'].append(page_id)
                    counts['seen'].append(page_id)
                    seen.add(page_id)
                    counts['found'] = len(seen)
                    save_job(conn,job_id,counts)
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
            counts['errors'].append(str(exc)[:240])
            failed = bool(search_recovery.record_failure(counts,exc,target={
                'stage':'commons','query_index':index}))
            if failed:
                counts['stopReason'] = '来源网络暂不可用，已保留结果，可在冷却后恢复'
        # Network recovery keeps the failed query cursor, including candidates
        # already saved before a download failed. Seen IDs remain deduplicated.
        counts['cursor'] = index if failed else index+1
        with closing(db()) as conn, conn:
            save_job(conn,job_id,counts)
        if failed:
            break
        if counts.get('resume_target',{}).get('stage') == 'commons':
            counts.pop('resume_target',None)
            counts['stopReason'] = '失败的来源查询已恢复，保留结果等待核验'
            break
    finish_video_job(db,media,body,job_id,counts)
    finish_search(db,job_id,body,counts)


def finish_search(db,job_id,body,counts):
    state = ('partial' if counts['found'] else 'failed') if counts['errors'] else 'done'
    with closing(db()) as conn, conn:
        if body.get('videoOnly'):
            counts['remainingGaps'],counts['verification'] = candidate_gaps(conn,body,counts['pageIds'])
        save_job(conn,job_id,counts,state)


def run_jobs(db, media, wake):
    while True:
        with closing(db()) as conn, conn:
            job = search_recovery.next_queued(conn)
            if job:
                conn.execute("UPDATE commons_job SET state='running' WHERE id=?",(job['id'],))
        if not job:
            wake.wait(1)
            wake.clear()
            continue
        body = json.loads(job['payload'])
        counts = dict(found=0,downloaded=0,lead=0,duplicate=0,errors=[],pageIds=[],seen=[],cursor=0)
        counts.update(json.loads(job['result']))
        if counts.get('resume_target',{}).get('stage') == 'supplemental':
            finish_video_job(db,media,body,job['id'],counts)
            finish_search(db,job['id'],body,counts)
            continue
        if 'retryPage' in body:
            page_id = body['retryPage']
            try:
                with closing(db()) as conn, conn:
                    counts[run_retry(conn,media,page_id)] += 1
                    counts.update(found=1,pageIds=[page_id])
                    save_job(conn,job['id'],counts,'done')
            except (OSError,ValueError,sqlite3.Error) as exc:
                counts['errors'].append(str(exc)[:240])
                with closing(db()) as conn, conn:
                    save_job(conn,job['id'],counts,'failed')
            continue
        if body.get('mode')=='agent':
            run_agent_job(db,media,job['id'],body,counts)
            continue
        run_fixed_job(db,media,job['id'],body,counts)

def job_payload(body, gap):
    from shot_search import CONCEPTS, PLACES, LINES, detect, normalize, search_fields
    fields = search_fields(body)
    script = fields['script']
    required = fields['required']
    manual = str(body.get('keywords','')).strip()[:180]
    rules = gap['rules'][1:]
    place_vocab = {**PLACES, **LINES}
    anchors = []
    for label in rules:
        if label in place_vocab:
            anchors.append(next((word for word in place_vocab[label] if word.isascii()),label))
        elif label in (body.get('location'),body.get('line')):
            anchors.append(label)
    english = {
        '车窗视角':'train window','湖泊':'loch lake','山地':'Highlands',
        '积雪':'snow','夜间':'night','清晨':'morning','发车':'departure',
        '站台':'platform','车站':'station','列车':'train','卧铺车厢':'sleeper cabin',
        '铁路高架桥':'railway viaduct'
    }
    must = detect(normalize(required),CONCEPTS)
    wanted = detect(normalize(script),CONCEPTS)
    anchor = ' '.join(dict.fromkeys(anchors))
    features = ' '.join(english[x] for x in sorted(must or wanted))
    context = ' '.join(english[x] for x in sorted(wanted-must-{'清晨','夜间'}))
    media_filter = ' filetype:video' if body.get('videoOnly') is True or float(body.get('seconds') or 0)>0 else ''
    raw_query = ' '.join(x for x in (anchor,features,context if must else '') if x)
    queries = list(dict.fromkeys(
        (search_agent.safe_query(q[:180],{'videoOnly':True}) if media_filter else q[:180]) for q in (manual or raw_query or script,
        ' '.join(x for x in (anchor,features) if x) or manual or script) if q))
    mode = body.get('mode','fixed')
    if mode not in ('fixed','agent'):
        raise ValueError('搜索模式必须是固定搜索或 Agent 搜索')
    result = dict(**search_recovery.external_metadata(body),mode=mode,anchor=anchor,videoOnly=bool(media_filter),supplementSources=True,script=script,purpose=body.get('purpose','internal'),rules=gap['rules'],
                gaps=gap['gaps'],queries=queries,search={key:body[key] for key in
                ('script','purpose','location','line','orientation','seconds','required','infer','videoOnly') if key in body})
    if is_reference_request(body):
        result['fullspec'] = {'script':script,'required':required}
    return result
