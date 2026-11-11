"""Group-hosted video review. Secrets are supplied only through server environment."""
import collections
import hashlib
import hmac
import json
import mimetypes
import os
import secrets
import tempfile
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from video_review import review_video

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD = 100 * 1024 * 1024
SESSION_SECRET = secrets.token_bytes(32)
PASSWORD = os.environ.get('TEAM_ACCESS_PASSWORD', '')
API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
PORT = int(os.environ.get('PORT', '8080'))
SECURE_COOKIE = os.environ.get('COOKIE_SECURE', '1') != '0'
review_slot = threading.BoundedSemaphore(1)
review_lock = threading.Lock()
review_times = collections.deque()
MAX_REVIEWS_PER_HOUR = int(os.environ.get('MAX_REVIEWS_PER_HOUR', '20'))
login_lock = threading.Lock()
login_times = collections.deque()


def make_cookie():
    expiry = str(int(time.time()) + 8 * 3600)
    mac = hmac.new(SESSION_SECRET, expiry.encode(), hashlib.sha256).hexdigest()
    return expiry + '.' + mac


def authenticated(header):
    try:
        cookies = SimpleCookie()
        cookies.load(header or '')
        token = cookies['team_session'].value
        expiry, signature = token.split('.', 1)
        valid = hmac.new(SESSION_SECRET, expiry.encode(), hashlib.sha256).hexdigest()
        return int(expiry) > time.time() and hmac.compare_digest(signature, valid)
    except (KeyError, ValueError, TypeError):
        return False


def demo_result():
    # The repository already contains this static example; no per-user output is loaded.
    source = (ROOT / 'index.html').read_text(encoding='utf-8')
    start = source.index('const R = ') + len('const R = ')
    end = source.index(';\nconst STUDIO =', start)
    result = source[start:end]
    json.loads(result)  # Reject invalid generated content.
    return result


def page():
    tpl = (ROOT / 'web/template.html').read_text(encoding='utf-8')
    return (tpl.replace('/*__RESULT__*/null', demo_result())
               .replace('/*__STUDIO__*/false', 'true')
               .replace('/*__HOSTED__*/false', 'true'))


LOGIN = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>小组视频评测</title><body style="background:#101318;color:#eee;font:16px system-ui;max-width:400px;margin:12vh auto;padding:24px"><h1>小组视频评测</h1><p>请输入小组访问口令。</p><form method="post" action="/login"><input type="password" name="password" autocomplete="current-password" required style="padding:10px;max-width:100%"><button style="padding:10px" type="submit">进入</button></form><p>{}</p></body></html>'''


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *args):
        pass  # Never record credentials or user data.

    def send(self, status, data, content_type='application/json; charset=utf-8', extra=None):
        if isinstance(data, (dict, list)):
            data = json.dumps(data, ensure_ascii=False).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def same_origin(self):
        origin = self.headers.get('Origin', '')
        parsed = urlsplit(origin)
        return parsed.scheme in ('http', 'https') and parsed.netloc == self.headers.get('Host')

    def is_auth(self):
        return authenticated(self.headers.get('Cookie'))

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/health':
            return self.send(200, {'ok': True})
        if path == '/login':
            return self.send(200, LOGIN.format(''), 'text/html; charset=utf-8')
        if not self.is_auth():
            return self.send(303, '', extra={'Location': '/login'})
        if path in ('/', '/index.html'):
            return self.send(200, page(), 'text/html; charset=utf-8')
        if path == '/api/keys':
            return self.send(200, {'deepseek': True, 'minimax': False})
        # Read only checked-in demo assets, never local configuration or other files.
        parts = path.lstrip('/').split('/')
        if len(parts) == 2 and parts[0] in ('clips', 'thumbs', 'audio') and parts[1] not in ('', '.', '..'):
            file = (ROOT / parts[0] / parts[1]).resolve()
            if file.parent == ROOT / parts[0] and file.is_file():
                size = file.stat().st_size
                raw_range = self.headers.get('Range', '')
                start, end, status = 0, size - 1, 200
                if raw_range:
                    try:
                        if not raw_range.startswith('bytes=') or ',' in raw_range:
                            raise ValueError()
                        a, b = raw_range[6:].split('-', 1)
                        if not a:
                            raise ValueError()
                        start, end, status = int(a), min(int(b) if b else end, end), 206
                        if start < 0 or start > end:
                            raise ValueError()
                    except ValueError:
                        return self.send(416, {'error': '无效的视频范围。'})
                self.send_response(status)
                self.send_header('Content-Type', mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
                self.send_header('Content-Length', str(end - start + 1))
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Content-Type-Options', 'nosniff')
                if status == 206:
                    self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.end_headers()
                with file.open('rb') as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining:
                        data = f.read(min(1024 * 1024, remaining))
                        if not data:
                            break
                        self.wfile.write(data)
                        remaining -= len(data)
                return
        return self.send(404, {'error': '未找到文件。'})

    def do_POST(self):
        path = urlsplit(self.path).path
        if not self.same_origin():
            return self.send(403, {'error': '请求来源不正确。'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            return self.send(400, {'error': '请求大小无效。'})
        if path == '/login':
            if not 0 < size <= 4096:
                return self.send(400, '请求无效', 'text/plain; charset=utf-8')
            with login_lock:
                now = time.monotonic()
                while login_times and now - login_times[0] > 60:
                    login_times.popleft()
                if len(login_times) >= 10:
                    return self.send(429, '尝试过多，请稍后重试。', 'text/plain; charset=utf-8')
                login_times.append(now)
            try:
                deadline = time.monotonic() + 20
                chunks = []
                remaining = size
                while remaining:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError()
                    self.connection.settimeout(min(30, left))
                    chunk = self.rfile.read1(min(4096, remaining))
                    if not chunk:
                        raise OSError('请求不完整')
                    chunks.append(chunk)
                    remaining -= len(chunk)
                self.connection.settimeout(30)
                form = parse_qs(b''.join(chunks).decode('utf-8'))
                supplied = form.get('password', [''])[0]
            except (UnicodeError, OSError):
                supplied = ''
            if not hmac.compare_digest(supplied.encode('utf-8'), PASSWORD.encode('utf-8')):
                return self.send(401, LOGIN.format('口令不正确。'), 'text/html; charset=utf-8')
            flags = '; Secure' if SECURE_COOKIE else ''
            return self.send(303, '', extra={'Location': '/', 'Set-Cookie': f'team_session={make_cookie()}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{flags}'})
        if not self.is_auth():
            return self.send(401, {'error': '请先输入小组口令。'})
        if path != '/api/video-review':
            return self.send(404, {'error': '云端仅支持成片评测，不支持生成或写入剪映草稿。'})
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/octet-stream':
            return self.send(415, {'error': '视频上传格式不正确。'})
        if not 0 < size <= MAX_UPLOAD:
            return self.send(400, {'error': '视频需小于 100 MB。'})
        if not review_slot.acquire(blocking=False):
            return self.send(429, {'error': '当前有视频正在评测，请稍后再试。'})
        try:
            with review_lock:
                now = time.monotonic()
                while review_times and now - review_times[0] > 3600:
                    review_times.popleft()
                if MAX_REVIEWS_PER_HOUR <= 0 or len(review_times) >= MAX_REVIEWS_PER_HOUR:
                    return self.send(429, {'error': '本小时评测次数已达上限，请稍后重试。'})
                review_times.append(now)
            with tempfile.TemporaryDirectory(prefix='team-video-review-') as tmp:
                path = os.path.join(tmp, 'video')
                with open(path, 'wb') as f:
                    remaining = size
                    deadline = time.monotonic() + 180
                    while remaining:
                        left = deadline - time.monotonic()
                        if left <= 0:
                            raise TimeoutError()
                        self.connection.settimeout(min(30, left))
                        block = self.rfile.read1(min(65536, remaining))
                        if not block:
                            raise ValueError('视频上传未完成。')
                        f.write(block)
                        remaining -= len(block)
                self.connection.settimeout(30)
                review = review_video(path, API_KEY, os.environ.get('DEEPSEEK_VIDEO_MODEL', 'deepseek-flash'))
                review['limitations'] = [line.replace('语音在本地转成文字', '语音在服务端转成文字') for line in review.get('limitations', [])]
            return self.send(200, {'review': review})
        except TimeoutError:
            return self.send(408, {'error': '上传超时，请重试。'})
        except (ValueError, RuntimeError) as e:
            return self.send(400, {'error': str(e)})
        except Exception:
            return self.send(500, {'error': '评测失败，请检查服务状态后重试。'})
        finally:
            review_slot.release()


class LimitedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, *args, **kwargs):
        self.connections = threading.BoundedSemaphore(8)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()


if __name__ == '__main__':
    if len(PASSWORD) < 16 or not API_KEY:
        raise SystemExit('需在服务器环境中配置至少 16 字符的 TEAM_ACCESS_PASSWORD 和 DEEPSEEK_API_KEY。')
    # Ensure boot errors are explicit rather than serving an incomplete page.
    demo_result()
    LimitedHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
