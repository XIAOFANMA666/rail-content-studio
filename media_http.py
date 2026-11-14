"""HTTP boundary for the local media workflow. No key is returned to the browser."""
import mimetypes
import re
from urllib.parse import parse_qs, urlparse

import media_library as media


def handle_get(handler, root, out):
    parsed = urlparse(handler.path)
    path = parsed.path
    if not (path.startswith("/api/media/") or path.startswith("/output/media/")):
        return False
    service = media.service(root, out)
    try:
        params = {k: v[-1] for k, v in parse_qs(parsed.query).items()}
        if path == "/api/media/config":
            handler._send(200, service.config())
        elif path == "/api/media/list":
            handler._send(200, service.listing(params))
        elif path == "/api/media/detail":
            handler._send(200, service.media(params.get("id")))
        elif path == "/api/media/tasks":
            handler._send(200, {"items": service.tasks()})
        elif path == "/api/media/clips":
            handler._send(200, {"clips": media.approved_clips(root, out)})
        elif path.startswith("/output/media/"):
            stream_file(handler, service.file(path[len("/output/media/"):]))
        else:
            handler._send(404, {"error": "素材接口不存在"})
    except (media.MediaError, ValueError, TypeError):
        handler._send(400, {"error": "素材请求无效或文件不存在"})
    except Exception:
        handler._send(500, {"error": "素材数据读取失败，请检查本机存储"})
    return True


def handle_post(handler, root, out, port):
    path = urlparse(handler.path).path
    if not path.startswith("/api/media/"):
        return False
    if (handler.headers.get("Origin") not in ("http://localhost:%s" % port, "http://127.0.0.1:%s" % port)
        or handler.headers.get("Host") not in ("localhost:%s" % port, "127.0.0.1:%s" % port)):
        handler._send(403, {"error": "请从本地工作台操作素材库"})
        return True
    try:
        if handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise media.MediaError("请提交 JSON 请求")
        size = int(handler.headers.get("Content-Length", 0))
        if not 0 < size <= 32768:
            raise media.MediaError("请求为空或超过 32 KB")
        body = handler._json()
        if not isinstance(body, dict):
            raise media.MediaError("请求格式无效")
        service = media.service(root, out)
        if path == "/api/media/search":
            result = service.submit("search", body)
        elif path == "/api/media/analyze":
            result = service.submit("analyze", body)
        elif path == "/api/media/decide":
            result = service.decide(body)
        elif path == "/api/media/cancel":
            result = service.cancel(body.get("id"))
        elif path == "/api/media/retry":
            task = service.task(body.get("id"))
            if task["status"] in media.ACTIVE:
                raise media.MediaError("任务仍在处理")
            result = service.submit(task["kind"], task["request"])
        else:
            handler._send(404, {"error": "素材接口不存在"})
            return True
        handler._send(200, result)
    except (media.MediaError, ValueError, TypeError) as exc:
        handler._send(400, {"error": str(exc) if isinstance(exc, media.MediaError) else "素材请求格式无效"})
    except Exception:
        handler._send(500, {"error": "素材操作失败，已有记录保留，请检查本机存储"})
    return True


def stream_file(handler, path):
    size = path.stat().st_size
    start, end, code = 0, size - 1, 200
    value = handler.headers.get("Range")
    if value:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", value)
        if not match or not any(match.groups()):
            handler.send_response(416)
            handler.send_header("Content-Range", "bytes */%s" % size)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        a, b = match.groups()
        if not a:
            start = max(0, size - int(b))
        else:
            start = int(a)
            end = min(int(b), size - 1) if b else size - 1
        if start > end or start >= size:
            handler.send_response(416)
            handler.send_header("Content-Range", "bytes */%s" % size)
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return
        code = 206
    handler.send_response(code)
    handler.send_header("Content-Type", mimetypes.guess_type(str(path))[0] or "application/octet-stream")
    handler.send_header("Content-Length", str(end - start + 1))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Cache-Control", "private, no-store")
    if code == 206:
        handler.send_header("Content-Range", "bytes %s-%s/%s" % (start, end, size))
    handler.end_headers()
    try:
        with path.open("rb") as stream:
            stream.seek(start)
            remaining = end - start + 1
            while remaining:
                data = stream.read(min(256 * 1024, remaining))
                if not data:
                    break
                handler.wfile.write(data)
                remaining -= len(data)
    except (BrokenPipeError, ConnectionResetError):
        pass


def enrich_result(root, out, result):
    """Expose newly approved clips without rewriting a saved timeline or bundled demo."""
    clips = media.approved_clips(root, out)
    if not clips:
        return result
    result["clips"] = {**result.get("clips", {}), **{c["id"]: c for c in clips}}
    return result


def validate_swaps(root, out, result, swaps):
    """Old timelines retain their snapshots; a new swap must target an active asset."""
    active = {c["id"] for c in media.approved_clips(root, out)}
    if not isinstance(swaps, dict):
        raise media.MediaError("镜头修改格式不正确")
    for changes in swaps.values():
        if not isinstance(changes, dict):
            raise media.MediaError("镜头修改格式不正确")
        for cid in changes.values():
            if str(cid).startswith("px") and cid not in active:
                raise media.MediaError("素材已停用或文件缺失，请刷新素材库")
