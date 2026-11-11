"""Read the existing AIHOT public feed; never starts collection or model jobs."""
import json
import os
import re
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse
from urllib.request import urlopen

import llm


def http_url(value):
    value = str(value or "")
    parsed = urlparse(value)
    return value if parsed.scheme in ("http", "https") and parsed.netloc and not parsed.username and not parsed.password else ""


def read_json(url):
    with urlopen(url, timeout=4) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("feed too large")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("invalid feed")
    return data


def published_at(item):
    try:
        date = datetime.fromisoformat(str(item.get("publishedAt") or "").replace("Z", "+00:00"))
        return date if date.tzinfo else None
    except ValueError:
        return None


def video_platform(url):
    parsed = urlparse(http_url(url))
    host = (parsed.hostname or "").removeprefix("www.").removeprefix("m.")
    if host == "youtu.be" and parsed.path.strip("/"):
        return "YouTube"
    if host == "youtube.com" and (parsed.path == "/watch" and "v=" in parsed.query or parsed.path.startswith("/shorts/")):
        return "YouTube"
    if host == "tiktok.com" and re.match(r"^/@[^/]+/video/\d+", parsed.path):
        return "TikTok"
    if host == "instagram.com" and re.match(r"^/reels?/[^/]+", parsed.path):
        return "Instagram"
    return None


def read_feed(now=None):
    env = llm.load_env()
    api = http_url(os.environ.get("AIHOT_API_URL") or env.get("AIHOT_API_URL") or "http://127.0.0.1:3301").rstrip("/")
    site = http_url(os.environ.get("AIHOT_SITE_URL") or env.get("AIHOT_SITE_URL") or "http://127.0.0.1:3300").rstrip("/")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=24)
    result = {"recent": None, "history": None, "errors": {}, "site": site,
              "checked_at": now.isoformat(), "freshness": None,
              "platforms": [{"name": name, "status": "配置已填写 · 采集器待接" if os.environ.get(key) or env.get(key) else "待配置数据接口"}
                            for name, key in (("YouTube", "YOUTUBE_API_KEY"), ("TikTok", "VIDEO_TRENDS_TIKTOK_FEED_URL"), ("Instagram", "VIDEO_TRENDS_INSTAGRAM_FEED_URL"))]}
    if not api or not site:
        result["errors"]["config"] = "AIHOT 地址无效，请在本机 .env 填写 HTTP 或 HTTPS 地址。"
        return result

    def normalize(item):
        ident = str(item.get("id") or "")
        title = item.get("title")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident) or not isinstance(title, str) or not title.strip():
            return None
        source = item.get("source") or {}
        links = item.get("links") or {}
        platform = video_platform(links.get("original"))
        if not platform:
            return None
        return {"id": ident, "title": title[:300], "summary": str(item.get("summary") or "")[:1000],
                "platform": platform,
                "source": str(source.get("name") or "未标注来源")[:200],
                "published_at": published_at(item).isoformat() if published_at(item) else None,
                "url": site + "/items/" + ident, "original": http_url(links.get("original"))}

    for kind, path in (("recent", "/api/v1/items?window=24h&by=published&mode=all&limit=30"),
                       ("history", "/api/site/pool?tab=time")):
        try:
            data = read_json(api + path)
            if not isinstance(data.get("items"), list):
                raise ValueError("invalid items")
            # Pool summaries omit original URLs. Resolve a bounded first page before
            # classifying videos; agency case studies must never become video hits.
            if kind == "history":
                def detail(item):
                    ident = str(item.get("id") or "") if isinstance(item, dict) else ""
                    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", ident):
                        return {}
                    return read_json(api + "/api/site/items/" + ident)
                with ThreadPoolExecutor(max_workers=3) as executor:
                    data["items"] = list(executor.map(detail, data["items"][:6]))
            rows = []
            seen = set()
            for item in data["items"]:
                if not isinstance(item, dict):
                    continue
                date = published_at(item)
                recent = date is not None and cutoff <= date <= now
                if kind == "recent" and not recent or kind == "history" and recent:
                    continue
                row = normalize(item)
                if row and row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
            result[kind] = rows[:6]
            if kind == "history":
                result["freshness"] = data.get("freshness")
        except (OSError, ValueError, TypeError, AttributeError):
            result["errors"][kind] = "暂时无法读取 AIHOT，请检查本地服务后重试。"
    return result
