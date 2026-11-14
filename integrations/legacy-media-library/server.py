import argparse
from contextlib import closing
from email import policy
from email.parser import BytesParser
import hashlib
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit
from shot_search import search
import commons
import search_recovery
import video_sources

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
MEDIA = DATA / 'media'
MEDIA.mkdir(parents=True, exist_ok=True)
TYPES = {'.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
         '.webp': 'image/webp', '.heic': 'image/heic', '.mp4': 'video/mp4',
         '.mov': 'video/quicktime', '.m4v': 'video/mp4'}

def db():
    conn = sqlite3.connect(DATA / 'library.sqlite3', timeout=15)
    conn.row_factory = sqlite3.Row
    return conn

def initialize():
    with closing(db()) as conn, conn:
        conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS import_batch(
            id INTEGER PRIMARY KEY, uploader TEXT NOT NULL, origin TEXT NOT NULL,
            items TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS asset(
            id TEXT PRIMARY KEY, name TEXT, extension TEXT, status TEXT,
            error TEXT DEFAULT '', info TEXT DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS source(
            id INTEGER PRIMARY KEY, asset TEXT, name TEXT, uploader TEXT,
            origin TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS segment(
            id INTEGER PRIMARY KEY, asset TEXT, payload TEXT, state TEXT DEFAULT 'pending',
            reviewer TEXT DEFAULT '', note TEXT DEFAULT '', updated TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS review_log(
            id INTEGER PRIMARY KEY, segment INTEGER, state TEXT, reviewer TEXT,
            note TEXT, payload TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        conn.executescript(commons.SCHEMA)
        conn.execute("UPDATE commons_job SET state='queued' WHERE state='running'")
        conn.execute("UPDATE asset SET status='queued' WHERE status='processing'")

wake = threading.Event()
commons_wake = threading.Event()

def prepare_video(folder):
    source = folder/'source.webm'
    target = folder/'original.mp4'
    if not source.is_file() or target.is_file():
        return
    bundled = sorted((DATA/'build/video-tools/imageio_ffmpeg/binaries').glob('ffmpeg-*'))
    executable = shutil.which('ffmpeg') or next((str(path) for path in bundled if path.is_file()),None)
    if not executable:
        raise ValueError('WebM 转码失败：本机缺少 FFmpeg')
    temporary = folder/'preview.tmp.mp4'
    try:
        result = subprocess.run([executable,'-nostdin','-hide_banner','-loglevel','error','-y',
            '-protocol_whitelist','file','-f','matroska','-i',str(source),'-map','0:v:0','-map','0:a:0?',
            '-c:v','libx264','-preset','fast','-pix_fmt','yuv420p','-vf','pad=ceil(iw/2)*2:ceil(ih/2)*2',
            '-c:a','aac','-movflags','+faststart',str(temporary)],capture_output=True,timeout=180)
        if result.returncode or not temporary.is_file() or not temporary.stat().st_size:
            raise ValueError('WebM 转码失败，请检查原文件或重试')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def process_jobs():
    while True:
        with closing(db()) as conn, conn:
            asset = conn.execute("SELECT * FROM asset WHERE status='queued' ORDER BY rowid LIMIT 1").fetchone()
            if asset:
                conn.execute("UPDATE asset SET status='processing' WHERE id=?", (asset['id'],))
        if not asset:
            wake.wait(1)
            wake.clear()
            continue
        key = asset['id']
        folder = MEDIA / key
        try:
            prepare_video(folder)
            result = subprocess.run([DATA/'build/media', folder/('original'+asset['extension']), folder],
                                    capture_output=True, text=True, timeout=180)
            if result.returncode:
                raise ValueError(result.stderr.strip() or '媒体无法解码')
            info = json.loads(result.stdout)
            parts = info.pop('parts')
            if not parts:
                raise ValueError('没有可预览的片段')
            with closing(db()) as conn, conn:
                conn.executemany('INSERT INTO segment(asset,payload) VALUES (?,?)',
                                 [(key, json.dumps(part, ensure_ascii=False)) for part in parts])
                conn.execute("UPDATE asset SET status='ready', info=? WHERE id=?", (json.dumps(info), key))
        except Exception as exc:
            with closing(db()) as conn, conn:
                conn.execute("UPDATE asset SET status='failed', error=? WHERE id=?", (str(exc)[:400], key))

class Handler(BaseHTTPRequestHandler):
    def reply(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if path == '/api/import-batches':
            with closing(db()) as conn:
                rows = conn.execute('SELECT * FROM import_batch ORDER BY id DESC')
                return self.reply([{**dict(r), 'items':json.loads(r['items'])} for r in rows])
        if path == '/api/commons/agent-status':
            return self.reply(commons.search_agent.status())
        if path == '/api/commons':
            with closing(db()) as conn:
                jobs = [{**dict(r), 'payload':json.loads(r['payload']), 'result':json.loads(r['result'])}
                        for r in conn.execute('SELECT * FROM commons_job ORDER BY id DESC LIMIT 30')]
                candidates = [{**dict(r),'payload':json.loads(r['payload']),'job':json.loads(r['job'])}
                              for r in conn.execute("""SELECT c.*,j.payload job FROM commons_candidate c
                                                       JOIN commons_job j ON j.id=c.job_id ORDER BY c.rowid DESC""")]
            return self.reply({'jobs':jobs,'candidates':candidates})
        if path == '/api/library':
            with closing(db()) as conn:
                assets = [dict(row) for row in conn.execute('SELECT * FROM asset ORDER BY rowid DESC')]
                for asset in assets:
                    key = asset['id']
                    asset['info'] = json.loads(asset['info'])
                    asset['sources'] = [dict(r) for r in conn.execute('SELECT * FROM source WHERE asset=? ORDER BY id', (key,))]
                    asset['segments'] = [
                        {**dict(r), 'payload': json.loads(r['payload'])}
                        for r in conn.execute('SELECT * FROM segment WHERE asset=? ORDER BY id', (key,))
                    ]
            return self.reply(assets)
        if re.fullmatch(r'/media/[a-f0-9]{64}/(?:original\.[a-z0-9]+|\d+\.jpg)', path):
            file = DATA / path.lstrip('/')
            mime = TYPES.get(file.suffix)
        else:
            allowed = {'/': ('index.html', 'text/html'), '/app.js': ('app.js', 'text/javascript'), '/style.css': ('style.css', 'text/css')}
            if path not in allowed:
                return self.reply({'error':'找不到页面'}, 404)
            name, mime = allowed[path]
            file = ROOT / 'dist' / name
        if not mime or not file.is_file():
            return self.reply({'error':'文件不存在'}, 404)
        size = file.stat().st_size
        start, end, status = 0, size-1, 200
        requested = self.headers.get('Range')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if match and any(match.groups()):
                first, last = match.groups()
                start = int(first) if first else max(0, size-int(last))
                end = min(size-1, int(last)) if first and last else size-1
                status = 206
            if not match or start > end or not any(match.groups()):
                self.send_response(416)
                self.send_header('Content-Range', f'bytes */{size}')
                self.end_headers()
                return
        self.send_response(status)
        self.send_header('Content-Type', mime + ('; charset=utf-8' if mime.startswith('text/') else ''))
        self.send_header('Content-Length', str(end-start+1))
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.end_headers()
        with file.open('rb') as stream:
            stream.seek(start)
            remaining = end-start+1
            while remaining:
                chunk = stream.read(min(remaining, 65536))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def do_POST(self):
        if self.headers.get('Origin') not in (None, 'http://' + self.headers.get('Host', '')):
            return self.reply({'error':'来源不匹配'}, 403)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 150*1024*1024:
                raise ValueError('请求为空或超过每批 150 MB 上限')
            path = urlsplit(self.path).path
            kind = self.headers.get('Content-Type','')
            if path == '/api/upload' and kind.startswith('multipart/form-data'):
                message = BytesParser(policy=policy.default).parsebytes(
                    ('Content-Type: '+kind+'\r\n\r\n').encode()+self.rfile.read(size))
                results, batch_id = self.upload(message)
                return self.reply({'id':batch_id,'items':results} if urlsplit(self.path).query=='batch=1' else results)
            if not kind.startswith('application/json') or size > 65536:
                raise ValueError('请求格式不正确')
            payload = json.loads(self.rfile.read(size))
            with closing(db()) as conn, conn:
                if path == '/api/commons/resume-search':
                    conn.execute('BEGIN IMMEDIATE')
                    try:
                        result = search_recovery.resume_search(conn, payload.get('id'))
                    except search_recovery.CooldownError as exc:
                        conn.commit()
                        return self.reply(exc.payload, 429)
                    conn.commit()
                    commons_wake.set()
                    return self.reply(result, 202)
                if path in ('/api/commons/start','/api/commons/retry'):
                    conn.execute('BEGIN IMMEDIATE')
                    if path.endswith('/start'):
                        metadata = search_recovery.external_metadata(payload)
                        existing = search_recovery.existing_request(conn, metadata)
                        if existing is not None:
                            return self.reply({'id':existing,'reused':True},202)
                    if conn.execute("SELECT count(*) FROM commons_job WHERE state IN ('queued','running')").fetchone()[0]>=5:
                        raise ValueError('已有 5 个任务在处理，请稍后再试')
                    if path.endswith('/retry'):
                        job = commons.retry_payload(conn,payload['id'])
                    else:
                        gap = search(conn,payload)
                        if gap['matches'] and payload.get('direct') is not True:
                            raise ValueError('当前条件已有可用镜头。请先查找需要补齐的条件。')
                        job = commons.job_payload(payload,gap)
                    job_id = conn.execute('INSERT INTO commons_job(payload) VALUES (?)',
                                          (json.dumps(job,ensure_ascii=False),)).lastrowid
                    conn.commit()
                    commons_wake.set()
                    return self.reply({'id':job_id},202)
                if path == '/api/commons/submit-link':
                    url = video_sources.source_url(payload['url'])
                    title = str(payload.get('title','')).strip()[:200] or url
                    key,info = video_sources.lead(url,title,'手动提交',
                        author=str(payload.get('author','')).strip()[:200],
                        license=str(payload.get('license','')).strip()[:300],
                        licenseUrl=video_sources.source_url(payload['licenseUrl']) if payload.get('licenseUrl') else '',
                        description=str(payload.get('note','')).strip()[:1000],mime='video/unknown')
                    original = conn.execute('SELECT payload FROM commons_job WHERE id=?',(payload.get('job'),)).fetchone()
                    if original:
                        job = json.loads(original['payload'])
                    else:
                        job = {'script':title,'mode':'fixed','queries':[],'rules':['视频网页线索'],'gaps':[]}
                    job = {**job,'mode':'fixed','queries':[]}
                    job.pop('retryPage',None)
                    job_id = conn.execute("INSERT INTO commons_job(payload,state) VALUES (?,'done')",
                                         (json.dumps(job,ensure_ascii=False),)).lastrowid
                    added = commons.store_lead(conn,job_id,key,info,'手动提交，待核对画面、来源及授权。')
                    counts = dict(found=0,downloaded=0,lead=0,duplicate=0,seen=[],pageIds=[])
                    commons.record_candidate(counts,key,'lead' if added else 'duplicate')
                    commons.save_job(conn,job_id,counts,'done')
                    conn.commit()
                    return self.reply({'id':job_id,'candidate':key},201)
                if path == '/api/commons/link':
                    asset_id = payload['asset']
                    if not conn.execute("SELECT 1 FROM asset WHERE id=? AND status='ready'",(asset_id,)).fetchone():
                        raise ValueError('请先导入并处理已核对的对应文件')
                    changed = conn.execute('UPDATE commons_candidate SET asset=? WHERE page_id=? AND asset IS NULL',
                                           (asset_id,payload['id'])).rowcount
                    if not changed:
                        raise ValueError('网页线索不存在或已关联文件')
                elif path == '/api/commons/lead':
                    state = payload['state']
                    if state not in ('hold','rejected'):
                        raise ValueError('网页线索只能待补证或拒绝')
                    note = str(payload.get('note','')).strip()[:1000]
                    if not note:
                        raise ValueError('请填写处理理由')
                    changed = conn.execute('UPDATE commons_candidate SET state=?,note=? WHERE page_id=? AND asset IS NULL',
                                           (state,note,payload['id'])).rowcount
                    if not changed:
                        raise ValueError('网页线索不存在或已关联文件')
                elif path == '/api/search':
                    return self.reply(search(conn, payload))
                elif path == '/api/retry':
                    changed = conn.execute("UPDATE asset SET status='queued', error='' WHERE id=? AND status='failed'", (payload['id'],)).rowcount
                    if not changed:
                        raise ValueError('只有失败项可以重试')
                elif path == '/api/review':
                    self.review(conn, payload)
                else:
                    return self.reply({'error':'未知操作'}, 404)
            wake.set()
            self.reply({'ok':True})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            self.reply({'error':str(exc)}, 400)

    def upload(self, message):
        fields, files = {}, []
        for part in message.iter_parts():
            name = part.get_param('name', header='content-disposition')
            if name == 'files':
                files.append(part)
            elif name in ('origin','uploader'):
                fields[name] = part.get_payload(decode=True).decode('utf-8','replace').strip()[:2000]
        if not 1 <= len(files) <= 20:
            raise ValueError('每批请选择 1–20 个文件')
        results = []
        with closing(db()) as conn, conn:
            conn.execute('BEGIN IMMEDIATE')
            for part in files:
                name = Path(part.get_filename() or '').name
                extension = Path(name).suffix.lower()
                blob = part.get_payload(decode=True)
                if extension not in TYPES or not blob or len(blob) > 50*1024*1024:
                    results.append({'name':name,'error':'文件为空、格式不支持或超过 50 MB'})
                    continue
                key = hashlib.sha256(blob).hexdigest()
                exists = conn.execute('SELECT 1 FROM asset WHERE id=?',(key,)).fetchone()
                if not exists:
                    folder = MEDIA / key
                    folder.mkdir(exist_ok=True)
                    original = folder / ('original'+extension)
                    original.write_bytes(blob)
                    conn.execute("INSERT INTO asset(id,name,extension,status) VALUES (?,?,?,'queued')",(key,name,extension))
                conn.execute('INSERT INTO source(asset,name,uploader,origin) VALUES (?,?,?,?)',
                             (key,name,fields.get('uploader',''),fields.get('origin','')))
                results.append({'name':name,'duplicate':bool(exists),'id':key})
            batch_id = conn.execute('INSERT INTO import_batch(uploader,origin,items) VALUES (?,?,?)',
                                    (fields.get('uploader',''),fields.get('origin',''),
                                     json.dumps(results,ensure_ascii=False))).lastrowid
        wake.set()
        return results, batch_id

    def review(self, conn, incoming):
        conn.execute('BEGIN IMMEDIATE')
        row = conn.execute('SELECT s.*, a.info FROM segment s JOIN asset a ON a.id=s.asset WHERE s.id=?',(incoming['id'],)).fetchone()
        if not row:
            raise ValueError('找不到片段')
        state = incoming['state']
        if state not in ('approved','hold','rejected','pending'):
            raise ValueError('无效的审核动作')
        data = json.loads(row['payload'])
        for field in ('description','location','locationEvidence','line','author','originalUrl','license','usage','riskNotes'):
            data[field] = str(incoming.get(field,'')).strip()[:2000]
        data['tags'] = list(dict.fromkeys(t.strip()[:80] for t in incoming.get('tags',[]) if t.strip()))[:30]
        if data['location'] and not data['locationEvidence']:
            raise ValueError('核实地点需要填写依据')
        duration = json.loads(row['info'])['duration']
        start, end = float(incoming['start']), float(incoming['end'])
        if duration and not 0 <= start < end <= duration + 0.000001:
            raise ValueError('片段范围必须位于原视频内')
        if duration:
            data.update(start=start, end=min(end,duration))
        data['authorizationVerified'] = incoming.get('authorizationVerified') is True
        data['publicationApproved'] = state == 'approved' and incoming.get('publicationApproved') is True
        reviewer = str(incoming.get('reviewer','')).strip()[:100]
        note = str(incoming.get('note','')).strip()[:2000]
        if state != 'pending' and not reviewer:
            raise ValueError('请填写审核人')
        if state in ('hold','rejected') and not note:
            raise ValueError('请填写待补证或拒绝原因')
        if state == 'approved' and not all((data['description'],data['tags'],data['license'],data['usage'],data['authorizationVerified'])):
            raise ValueError('正式入库需要描述、标签、授权凭据、使用范围和人工确认')
        body = json.dumps(data,ensure_ascii=False)
        conn.execute('UPDATE segment SET payload=?,state=?,reviewer=?,note=?,updated=CURRENT_TIMESTAMP WHERE id=?',
                     (body,state,reviewer,note,row['id']))
        conn.execute('INSERT INTO review_log(segment,state,reviewer,note,payload) VALUES (?,?,?,?,?)',
                     (row['id'],state,reviewer,note,body))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    port = parser.parse_args().port
    process_lock = (DATA / 'worker.lock').open('a')
    try:
        fcntl.flock(process_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.exit(1,'素材库已经运行，请先停止现有服务。\n')
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    initialize()
    threading.Thread(target=process_jobs,daemon=True).start()
    threading.Thread(target=commons.run_jobs,args=(db,MEDIA,commons_wake),daemon=True).start()
    print(f'本地素材库 http://127.0.0.1:{port}/',flush=True)
    server.serve_forever()
