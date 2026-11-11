"""本地工作台：在浏览器里输入选题 → 现场跑流水线 → 预览 → 换镜头 → 写回剪映草稿。

用法：.venv/bin/python studio.py   然后打开 http://localhost:8770
API key 放在 .env（DeepSeek 写脚本，MiniMax 配音），只在本机用，不会进网页。
"""
import json
import mimetypes
import os
import re
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

import llm
import script_store
import script_edit

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "output")
PORT = int(os.environ.get("PORT", 8770))
job = {"running": False, "log": [], "error": None}
lock = threading.Lock()


def read_result():
    """Fresh clones can preview the bundled run without local output or API keys."""
    path = os.path.join(OUT, "result.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(os.path.join(ROOT, "index.html"), encoding="utf-8") as f:
        match = re.search(r"const R = (.*?);\nconst STUDIO =", f.read(), re.S)
    if not match:
        raise ValueError("演示数据缺失，请重新拉取完整仓库。")
    result = json.loads(match.group(1))
    result["preview_only"] = True
    return result


def read_script_session():
    return script_store.current(OUT)


def page():
    tpl = open(os.path.join(ROOT, "web", "template.html"), encoding="utf-8").read()
    result = json.dumps(read_result(), ensure_ascii=False)
    review_path = os.path.join(ROOT, "data", "review", "review.json")
    review = open(review_path, encoding="utf-8").read() if os.path.exists(review_path) else "null"
    return (tpl.replace("/*__RESULT__*/null", result).replace("/*__REVIEW__*/null", review)
               .replace("/*__STUDIO__*/false", "true")
               .replace("/*__SCRIPT_SESSION__*/null", json.dumps(read_script_session(), ensure_ascii=False).replace("<", "\\u003c")))


def run_pipeline(args):
    """后台跑 pipeline.py，把每一步的日志收进 job，网页轮询 /api/status 显示真实进度。"""
    cmd = [sys.executable, "-u", os.path.join(ROOT, "pipeline.py")] + args
    p = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    tail = []
    for line in p.stdout:
        line = line.rstrip()
        tail = (tail + [line])[-15:]
        if "｜" in line and line.startswith("["):
            ms, rest = line[1:].split("]", 1)
            step, msg = rest.strip().split("｜", 1)
            with lock:
                job["log"].append({"step": step, "msg": msg, "ms": int(ms.replace("ms", "").strip())})
    p.wait()
    with lock:
        job["running"] = False
        if p.returncode != 0:
            err = next((l for l in reversed(tail) if "Error" in l or "错误" in l or "key" in l), tail[-1] if tail else "未知错误")
            job["error"] = err


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else (json.dumps(body, ensure_ascii=False) if not isinstance(body, str) else body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path in ("/", "/index.html"):
            return self._send(200, page(), "text/html; charset=utf-8")
        if path == "/api/script-library":
            return self._send(200, {"sessions": script_store.summaries(OUT), "vocabulary": script_edit.vocabulary()})
        if path == "/api/status":
            with lock:
                return self._send(200, dict(job))
        if path == "/api/keys":
            return self._send(200, {"deepseek": bool(llm.model_config("script")["api_key"]), "minimax": llm.has("MINIMAX_API_KEY"), "video": bool(llm.model_config("video")["api_key"]),
                                    "models": llm.models_in_use()})
        # 静态文件：clips/、thumbs/；audio/ 直接读 output/tts/（每次生成都是新的）
        if path.startswith("/audio/"):
            audio_root = os.path.join(OUT, "tts") if os.path.isfile(os.path.join(OUT, "result.json")) else os.path.join(ROOT, "audio")
            f = os.path.join(audio_root, os.path.basename(path))
        elif path.startswith(("/clips/", "/thumbs/")):
            f = os.path.join(ROOT, path.lstrip("/"))
        else:
            return self._send(404, {"error": "not found"})
        if not os.path.isfile(f):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(f)[0] or "application/octet-stream"
        size = os.path.getsize(f)
        rng = self.headers.get("Range")
        with open(f, "rb") as fh:
            if rng and rng.startswith("bytes="):  # 视频拖动需要分段读取
                a, _, b = rng[6:].partition("-")
                a = int(a or 0)
                b = int(b) if b else size - 1
                fh.seek(a)
                data = fh.read(b - a + 1)
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
            else:
                data = fh.read()
                self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/video-review":
            # 只接受工作台自己发起的请求，避免其他网站借本机账号触发付费请求。
            if self.headers.get("Origin") not in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"):
                return self._send(403, {"error": "请从本地工作台发起评测。"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/octet-stream":
                return self._send(415, {"error": "视频上传格式不正确。"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return self._send(400, {"error": "视频大小无效。"})
            if size <= 0 or size > 100 * 1024 * 1024:
                return self._send(400, {"error": "请选择不超过 100 MB 的视频。"})
            cfg = llm.model_config("video")   # 成片评测用哪家哪个模型，在 .env 里配
            if not cfg["api_key"]:
                return self._send(400, {"error": "请先在 .env 中填写 DEEPSEEK_API_KEY（或 VIDEO_API_KEY）。"})
            try:
                from video_review import review_video
                with tempfile.TemporaryDirectory(prefix="roughcut-review-") as tmp:
                    video_path = os.path.join(tmp, "video")
                    with open(video_path, "wb") as f:
                        remaining = size
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("视频上传未完成。")
                            f.write(chunk)
                            remaining -= len(chunk)
                    # 带上最近一次粗剪的数据：镜头、字幕、花字、脚本审核这些能直接算分，不用 AI 猜。
                    try:
                        with open(os.path.join(OUT, "result.json"), encoding="utf-8") as f:
                            draft = json.load(f)
                    except (OSError, ValueError):
                        draft = None
                    review = review_video(video_path, cfg["api_key"], cfg["model"], draft, cfg["base_url"])
                    review["usage"]["provider"] = cfg["provider"]
                return self._send(200, {"review": review})
            except (ValueError, RuntimeError) as e:
                return self._send(400, {"error": str(e)})
            except Exception:
                return self._send(500, {"error": "评测失败，请检查依赖和服务配置后重试。"})
        if path in ("/api/scripts", "/api/produce", "/api/scripts/open", "/api/scripts/save", "/api/scripts/rewrite", "/api/scripts/import"):
            if self.headers.get("Origin") not in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}"):
                return self._send(403, {"error": "请从本地工作台操作。"})
            try:
                if not 0 < int(self.headers.get("Content-Length", 0)) <= 131072:
                    raise ValueError("脚本请求过大或为空。")
                body = self._json()
                if not isinstance(body, dict):
                    raise ValueError("脚本请求格式不正确。")
            except (ValueError, TypeError):
                return self._send(400, {"error": "脚本请求格式不正确或超过 128 KB。"})
            with lock:
                if job["running"]:
                    return self._send(409, {"error": "已有生成任务正在进行。"})
                job.update(running=True, log=[], error=None)
            try:
                if path == "/api/scripts":
                    topic = (body.get("topic") or "").strip()[:80]
                    if not topic:
                        raise ValueError("请先输入选题。")
                    if not llm.model_config("script")["api_key"]:
                        raise ValueError("请先配置脚本模型密钥。")
                    from pipeline import generate_script_session
                    session = generate_script_session(topic, body.get("market") or "英国", body.get("platform") or "TikTok")
                    session = script_store.add(OUT, session)
                    with lock:
                        job["running"] = False
                    return self._send(200, session)
                if path == "/api/scripts/import":
                    import copy
                    import uuid
                    result = read_result()
                    session = {"id": uuid.uuid4().hex, "title": result["title"], "country": result["country"],
                               "platform": result["platform"], "usage": {"provider": "存档", "model": "存档"},
                               "scripts": copy.deepcopy(result["scripts"])}
                    session["scripts"]["chosen"] = None
                    session = script_store.add(OUT, session)
                    with lock:
                        job["running"] = False
                    return self._send(200, session)
                if path == "/api/scripts/open":
                    session = script_store.open_session(OUT, body.get("session_id"), body.get("variant"))
                    with lock:
                        job["running"] = False
                    return self._send(200, session)
                session = read_script_session()
                if not session or session["id"] != body.get("session_id"):
                    raise ValueError("脚本已更新，请刷新后重新选择。")
                if body.get("variant") not in [v["id"] for v in session["scripts"]["variants"]]:
                    raise ValueError("请先选择一个脚本版本。")
                if path == "/api/scripts/rewrite":
                    if not llm.model_config("script")["api_key"]:
                        raise ValueError("请先配置脚本模型密钥；手动编辑和保存无需密钥。")
                    response = script_edit.rewrite(session, body["variant"], body.get("instruction"))
                    with lock:
                        job["running"] = False
                    return self._send(200, response)
                if path == "/api/scripts/save":
                    variant = script_edit.validate(body.get("content"), body["variant"])
                    title = script_edit.text(body.get("title"), "选题名称", 80)
                    session = script_store.revise(OUT, session, variant, title, body.get("expected_head"),
                                                  "AI 改写" if body.get("action") == "rewrite" else "手动编辑")
                    with lock:
                        job["running"] = False
                    return self._send(200, session)
                # Pin the exact saved revision for this run; later library changes cannot alter it.
                session = script_store.open_session(OUT, session["id"], body["variant"])
                production_file = os.path.join(OUT, "production-script.json")
                script_store.atomic_json(production_file, session)
                args = ["--script-file", production_file, "--variant", body["variant"]]
                threading.Thread(target=run_pipeline, args=(args,), daemon=True).start()
                return self._send(200, {"ok": True})
            except Exception as e:
                with lock:
                    job["running"] = False
                message = str(e) if isinstance(e, ValueError) else "脚本操作失败，原稿仍保留。请检查模型配置或本地存储后重试。"
                if re.search(r"返回 402[：:]", str(e)):
                    message = "模型账户余额不足，请充值后重试。已有脚本仍可选用并制作粗剪。"
                return self._send(400, {"error": message})
        if path == "/api/run":
            body = self._json()
            topic = (body.get("topic") or "").strip()[:80]
            if not topic:
                return self._send(400, {"error": "请先输入选题"})
            if not llm.model_config("script")["api_key"]:
                return self._send(400, {"error": "还没填写脚本用的 key：请在 .env 里填 DEEPSEEK_API_KEY（或 SCRIPT_API_KEY）"})
            with lock:
                if job["running"]:
                    return self._send(409, {"error": "上一个还在生成"})
                job.update(running=True, log=[], error=None)
            args = ["--topic", topic, "--market", body.get("market") or "英国", "--platform", body.get("platform") or "TikTok"]
            threading.Thread(target=run_pipeline, args=(args,), daemon=True).start()
            return self._send(200, {"ok": True})
        if path == "/api/redraft":
            if not os.path.isfile(os.path.join(OUT, "result.json")):
                return self._send(409, {"error": "当前为随仓库附带的演示。请先生成本机草稿，再写入镜头修改。"})
            swaps = self._json().get("swaps") or {}
            sp = os.path.join(OUT, "swaps.json")
            json.dump(swaps, open(sp, "w"), ensure_ascii=False)
            r = subprocess.run([sys.executable, os.path.join(ROOT, "pipeline.py"), "--redraft", sp], cwd=ROOT, capture_output=True, text=True)
            if r.returncode != 0:
                return self._send(500, {"error": (r.stderr or r.stdout)[-400:]})
            return self._send(200, {"ok": True, "msg": r.stdout.strip()})
        return self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print(f"AI 粗剪工作台：http://localhost:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
