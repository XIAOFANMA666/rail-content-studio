"""Local script library. Content revisions are append-only; writes are atomic.

The server serializes mutations with its job lock. Legacy script-session.json
is imported on the first write and remains untouched as a recoverable source.
"""
import copy
import json
import os
import tempfile
import uuid
from datetime import datetime, timezone


def atomic_json(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read(out):
    path = os.path.join(out, "script-library.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    legacy = os.path.join(out, "script-session.json")
    if os.path.isfile(legacy):
        with open(legacy, encoding="utf-8") as f:
            session = json.load(f)
        session.update(project_id=session["id"], revision=1, action="原稿", created_at="")
        return {"current": session["id"], "sessions": [session]}
    return {"current": None, "sessions": []}


def current(out):
    library = read(out)
    return next((s for s in library["sessions"] if s["id"] == library["current"]), None)


def summaries(out):
    return [{k: s.get(k) for k in ("id", "project_id", "revision", "title", "country", "platform", "created_at", "action", "parent_id")}
            for s in reversed(read(out)["sessions"])]


def commit(out, library):
    atomic_json(os.path.join(out, "script-library.json"), library)


def add(out, session):
    library = read(out)
    session = copy.deepcopy(session)
    session.update(project_id=session["id"], revision=1, action="生成", created_at=datetime.now(timezone.utc).isoformat())
    library["sessions"].append(session)
    library["current"] = session["id"]
    commit(out, library)
    return session


def open_session(out, session_id, chosen=None):
    library = read(out)
    session = next((s for s in library["sessions"] if s["id"] == session_id), None)
    if session is None:
        raise ValueError("找不到这份脚本，请刷新脚本库。")
    if chosen is not None:
        if chosen not in [v["id"] for v in session["scripts"]["variants"]]:
            raise ValueError("请选择有效的脚本版本。")
        session["scripts"]["chosen"] = chosen
    library["current"] = session_id
    commit(out, library)
    return session


def revise(out, base, variant, title, expected_head, action):
    library = read(out)
    versions = [s for s in library["sessions"] if s["project_id"] == base["project_id"]]
    if versions[-1]["id"] != expected_head:
        raise ValueError("这组选题已在其他页面更新。请刷新脚本库后再保存；编辑内容仍保留在当前窗口。")
    session = copy.deepcopy(base)
    session.update(id=uuid.uuid4().hex, title=title, parent_id=base["id"],
                   revision=versions[-1]["revision"] + 1, action=action,
                   created_at=datetime.now(timezone.utc).isoformat())
    session["scripts"]["variants"] = [variant if v["id"] == variant["id"] else v for v in session["scripts"]["variants"]]
    session["scripts"]["chosen"] = None
    library["sessions"].append(session)
    library["current"] = session["id"]
    commit(out, library)
    return session
