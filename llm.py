"""大模型接口。写脚本和成片评测各用哪家、哪个模型，都在 .env 里配置；配音用 MiniMax。只用标准库。"""
import json
import os
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_env():
    path = os.path.join(ROOT, ".env")
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_VIDEO_MODEL", "MINIMAX_API_KEY", "MINIMAX_BASE_URL", "MINIMAX_GROUP_ID", "MINIMAX_TTS_MODEL",
              "LLM_BASE_URL", "LLM_API_KEY", "SCRIPT_BASE_URL", "SCRIPT_API_KEY", "SCRIPT_MODEL",
              "VIDEO_BASE_URL", "VIDEO_API_KEY", "VIDEO_MODEL", "PEXELS_API_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def has(key):
    return bool(load_env().get(key))


def _post(url, headers, body, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"{url} 返回 {e.code}：{e.read().decode('utf-8', 'ignore')[:300]}")


# ---------------- 模型配置：换模型只改 .env，不改代码 ----------------
# 两个环节各自可配：script（写脚本）和 video（成片评测，要能看图）。
# 查找顺序：<环节>_XXX → LLM_XXX → DeepSeek 默认值。接口要兼容 OpenAI 的 /chat/completions 格式
# （DeepSeek、通义千问兼容模式、OpenAI、MiniMax 等都是）。
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODELS = {"script": "deepseek-chat", "video": "deepseek-flash"}
PROVIDERS = {"deepseek": "DeepSeek", "dashscope": "通义千问", "aliyuncs": "通义千问", "openai": "OpenAI", "anthropic": "Claude",
             "minimax": "MiniMax", "moonshot": "Kimi", "bigmodel": "智谱", "volces": "豆包", "siliconflow": "硅基流动"}


def provider_label(base_url):
    host = urlparse(base_url).hostname or base_url
    return next((name for key, name in PROVIDERS.items() if key in host), host)


def model_config(role):
    """role 是 "script" 或 "video"。返回 {base_url, api_key, model, provider}。"""
    env, prefix = load_env(), role.upper()
    base = (env.get(prefix + "_BASE_URL") or env.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
    key = env.get(prefix + "_API_KEY") or env.get("LLM_API_KEY") or env.get("DEEPSEEK_API_KEY")
    legacy = env.get("DEEPSEEK_VIDEO_MODEL") if role == "video" else None   # 旧配置名，继续认
    model = env.get(prefix + "_MODEL") or legacy or DEFAULT_MODELS[role]
    return {"base_url": base, "api_key": key, "model": model, "provider": provider_label(base)}


def models_in_use():
    """给网页显示用，不含 key。"""
    env = load_env()
    out = {role: {k: v for k, v in model_config(role).items() if k in ("provider", "model")} for role in ("script", "video")}
    out["tts"] = {"provider": "MiniMax", "model": env.get("MINIMAX_TTS_MODEL") or "speech-2.8-hd"} if env.get("MINIMAX_API_KEY") else {"provider": "macOS 系统语音", "model": "say"}
    return out


def extra_params(base_url):
    """各家特有的参数。DeepSeek 要显式关掉思考模式才会直接出 JSON；别家不认这个参数，所以不发。"""
    return {"thinking": {"type": "disabled"}} if "deepseek" in (urlparse(base_url).hostname or "") else {}


def script_json(system, user, temperature=0.9):
    """让写脚本的模型按 JSON 返回。返回 (dict, usage)，usage 里带用了哪家哪个模型。"""
    cfg = model_config("script")
    if not cfg["api_key"]:
        raise RuntimeError("没有写脚本用的 key：请在 .env 里填 DEEPSEEK_API_KEY（或 SCRIPT_API_KEY）")
    t = time.time()
    body = {"model": cfg["model"], "temperature": temperature, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    url, headers = cfg["base_url"] + "/chat/completions", {"Authorization": f"Bearer {cfg['api_key']}"}
    try:
        r = _post(url, headers, body, timeout=180)
    except RuntimeError as e:
        if "response_format" not in str(e):
            raise
        body.pop("response_format")   # 有的模型不支持 JSON 模式，靠提示词约束输出格式
        r = _post(url, headers, body, timeout=180)
    content = r["choices"][0]["message"]["content"].strip()
    if content.startswith("```"):   # 有的模型会把 JSON 包在代码块里
        content = content.split("\n", 1)[1].rsplit("```", 1)[0]
    usage = r.get("usage", {})
    usage.update(seconds=round(time.time() - t, 1), provider=cfg["provider"], model=cfg["model"])
    return json.loads(content), usage


deepseek_json = script_json   # 旧名字，保留给老代码


# ---------------- MiniMax ----------------
MINIMAX_HOSTS = ["https://api.minimaxi.com", "https://api.minimax.cn", "https://api.minimax.io"]


def _minimax(path, body):
    env = load_env()
    key = env.get("MINIMAX_API_KEY")
    if not key:
        raise RuntimeError("没有 MiniMax key：请在 .env 里填 MINIMAX_API_KEY")
    first = env.get("MINIMAX_BASE_URL") or MINIMAX_HOSTS[0]
    hosts = [first.rstrip("/")] + [h for h in MINIMAX_HOSTS if h != first.rstrip("/")]
    gid = env.get("MINIMAX_GROUP_ID")
    last = None
    for h in hosts:
        url = h + path + (f"?GroupId={gid}" if gid else "")
        try:
            r = _post(url, {"Authorization": f"Bearer {key}"}, body)
        except Exception as e:  # 换个域名再试（国内 / 海外账号域名不同）
            last = e
            continue
        code = (r.get("base_resp") or {}).get("status_code", 0)
        if code == 1004:  # 鉴权失败，多半是域名和账号区域不匹配
            last = RuntimeError(f"MiniMax 鉴权失败（{h}）：{r['base_resp'].get('status_msg')}")
            continue
        if code != 0:
            raise RuntimeError(f"MiniMax 返回错误 {code}：{r['base_resp'].get('status_msg')}")
        return r
    raise last


def minimax_tts(text, out_path, voice_id, speed=1.0, model=None):
    """生成配音，写成 mp3。返回 (秒数, 计费字符数)。"""
    model = model or load_env().get("MINIMAX_TTS_MODEL") or "speech-2.8-hd"
    r = _minimax("/v1/t2a_v2", {
        "model": model, "text": text, "stream": False, "language_boost": "English",
        "voice_setting": {"voice_id": voice_id, "speed": speed, "vol": 1, "pitch": 0},
        "audio_setting": {"format": "mp3", "sample_rate": 44100, "bitrate": 128000, "channel": 1},
    })
    with open(out_path, "wb") as f:
        f.write(bytes.fromhex(r["data"]["audio"]))
    info = r.get("extra_info", {})
    return info.get("audio_length", 0) / 1000, info.get("usage_characters", len(text))


def minimax_voices():
    r = _minimax("/v1/get_voice", {"voice_type": "system"})
    return [v for v in r.get("system_voice", []) if v.get("voice_id", "").lower().startswith("english")]
