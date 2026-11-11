"""AI 粗剪流水线

选题 → 写脚本（脚本 Agent 初稿 + 规则审核）→ 读 Skill → 配音 → 分段 → 素材召回 → 规则检查 → 编排剪映草稿 → 自动评分

用法：
  .venv/bin/python pipeline.py                         # 示例：选题池打分 + 存档的 3 版脚本（不需要 key）
  .venv/bin/python pipeline.py --topic "巴黎坐火车去阿姆斯特丹"   # 输入选题：DeepSeek 现写脚本，MiniMax 配音
  .venv/bin/python pipeline.py --redraft swaps.json     # 按人工换过的镜头重新写剪映草稿
输出：output/result.json（给网页用）、output/log.txt、output/tts/*，以及剪映草稿文件夹。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import time

import pyJianYingDraft as jy

import llm
from draft_review import review_draft

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "output")
DEFAULT_DRAFT_ROOT = os.path.expanduser("~/Movies/JianyingPro/User Data/Projects/com.lveditor.draft")
DEMO_DRAFT_NAME = "AI粗剪_伦敦苏格兰卧铺_v1"
US = 1_000_000

# ---- 存档脚本用的关键词 → 概念表。DeepSeek 现写的脚本会自己给每句标概念，不走这张表 ----
LEXICON = {
    "sleep": ["sleeper"], "sleeper": ["sleeper", "train"], "asleep": ["sleeper"],
    "train": ["train"], "midnight": ["night"], "night": ["night"],
    "leaves": ["depart", "station"], "euston": ["station"], "station": ["station"],
    "city": ["city"], "wake": ["morning", "window"], "lochs": ["lake"], "loch": ["lake"],
    "highland": ["mountain"], "highlands": ["mountain"],
    "book": ["ticket"], "legs": ["ticket"], "ticket": ["ticket"], "save": ["ticket"],
    "app": ["app", "phone"], "search": ["app", "phone"], "code": ["app"],
    "london": ["london"], "evening": ["night"],
}
PLACES = {"london": "伦敦", "euston": "伦敦", "scotland": "苏格兰", "highland": "苏格兰", "highlands": "苏格兰"}
COUNTRIES = {"英国", "法国", "意大利", "荷兰", "瑞士", "德国", "西班牙", "葡萄牙", "奥地利", "比利时", "墨西哥"}

log_lines = []


def log(step, msg, t0):
    ms = int((time.time() - t0) * 1000)
    line = {"step": step, "msg": msg, "ms": ms}
    log_lines.append(line)
    print(f"[{ms:>6} ms] {step}｜{msg}", flush=True)


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def words(text):
    return re.findall(r"[a-z]+", text.lower())


def concepts_of(text):
    out = []
    for w in words(text):
        for c in LEXICON.get(w, []):
            if c not in out:
                out.append(c)
    return out


def places_of(text):
    return sorted({PLACES[w] for w in words(text) if w in PLACES})


def with_review_rates(topics):
    """数据复盘（review.py）写出了各题材的千播率，就拿它替换选题池里的示例爆款率。"""
    path = os.path.join(DATA, "review", "review.json")
    if not os.path.exists(path):
        return topics
    review = json.load(open(path, encoding="utf-8"))
    rates = {**topics["genre_hit_rate"], **review.get("genre_hit_rate", {})}
    works = sum(1 for r in review.get("rows", []) if r.get("views") is not None)
    return dict(topics, genre_hit_rate=rates,
                note=f"历史爆款率来自数据复盘：{works} 条作品的千播率（{review.get('latest')} 取数；发布记录和播放数是模拟的）。营销节点来自营销部门的年度 roadmap。")


def select_topic(topics):
    rates = topics["genre_hit_rate"]
    top_rate = max(rates.values())
    nodes = {n["name"]: n["weight"] for n in topics["nodes"]}
    scored = []
    for t in topics["pool"]:
        hit = max(rates[g] for g in t["genre"]) / top_rate
        sc = 0.5 * hit + 0.3 * nodes.get(t["node"], 0) + 0.2 * t["benefit_fit"]
        scored.append({**t, "hit": round(hit, 2), "node_w": nodes.get(t["node"], 0), "score": round(sc, 3)})
    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored


# ---------------- 写脚本 ----------------
def write_scripts_llm(topic, market, platform, facts, lib, brief=""):
    """DeepSeek 按提示词现写 3 版脚本。每句同时给出画面概念和地点，供素材召回用。"""
    prompt = open(os.path.join(DATA, "script_prompt.md"), encoding="utf-8").read()
    vocab = sorted({t for c in lib["clips"] for t in c["tags"]})
    places = sorted({p for c in lib["clips"] for p in c["places"]})
    system = prompt + f"""

## 输出格式（必须是 JSON）
{{"variants": [{{"id": "A", "idea": "这一版的开头思路（中文，一句话）",
  "lines": [{{"en": "英文口播", "zh": "中文对照", "role": "开头 · 冷知识 / 铺垫 · 路线 / 画面 · 情绪 / 利益点 · 省钱 / 结尾 · 搜码钩子",
             "on_screen": "需要花字时写大写英文，不需要写 null（第 1、4、5 句必须有）",
             "tags": ["这句话需要的画面概念，只能从概念表里选 1–3 个，最重要的放第一个"],
             "places": ["这句话说到的地点，只能从地点表里选；没说到地点就给空数组"]}}],
  "claims": ["这一版里提到的、需要人工核对的具体事实（时刻、时长、价格、班次），没有就给空数组"]}}]}}

概念表：{", ".join(vocab)}
地点表：{"、".join(places)}
三版要用不同的开头角度。"""
    user = json.dumps({
        "选题": topic, "目标观众所在国家": market, "发布平台": platform, "创作要求": brief,
        "事实库": [f["text"] for f in facts["facts"]],
        "提醒": "事实库里没有的班次、时刻、价格不要写；实在要写就写进 claims，交人工核对。优惠只能用事实库里的 up to 30%（分段买票）和搜码 UK20 额外 20%。不要提任何第三方品牌或公司名（铁路公司、App、食品等），我们自己的 App 就叫 the app。",
    }, ensure_ascii=False)
    data, usage = llm.script_json(system, user)
    variants = data["variants"][:3]
    for i, v in enumerate(variants):
        v["id"] = "ABC"[i]
        for ln in v["lines"]:
            ln["tags"] = [t for t in (ln.get("tags") or []) if t in vocab][:3]
            ln["places"] = [p for p in (ln.get("places") or []) if p in places]
            if not ln.get("on_screen") or str(ln["on_screen"]).lower() == "null":
                ln.pop("on_screen", None)
    return variants, usage, system, user


def check_script(variant, facts):
    lines = variant["lines"]
    text = " ".join(l["en"].lower() for l in lines)
    checks = []
    first = lines[0]["en"].lower()
    early = re.search(r"\d+%|code|save|off|uk\d+", first)
    checks.append({"name": "开头不放利益点（R1）", "ok": not early, "msg": "第 1 句没有价格或优惠" if not early else f"第 1 句出现利益点：{lines[0]['en']}"})
    long = [l["en"] for l in lines if len(l["en"].split()) > 14]
    checks.append({"name": "每句不超过 14 个词", "ok": not long, "msg": "都在 14 个词以内" if not long else f"{len(long)} 句超长：{long[0]}"})
    pct = [l["en"] for l in lines if "%" in l["en"] and not re.search(r"up to|extra|from", l["en"].lower())]
    checks.append({"name": "价格写 up to / extra（R4）", "ok": not pct, "msg": "价格表述合规" if not pct else f"有价格没写 up to：{pct[0]}"})
    conflict = [c["msg"] for c in facts["conflicts"] if all(w in text for w in c["if_all"])]
    claims = variant.get("claims") or []
    msg = conflict[0] if conflict else "没有和事实库冲突的说法"
    if not conflict and claims:
        msg += f"；另有 {len(claims)} 处具体事实要人工核对：{'；'.join(claims)}"
    checks.append({"name": "事实核查（对照事实库）", "ok": not conflict, "msg": msg})
    return checks


# ---------------- 配音 ----------------
def _to_wav_m4a(src, path_noext):
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@44100", src, path_noext + ".wav"], check=True)
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aac", src, path_noext + ".m4a"], check=True)
    info = subprocess.run(["afinfo", path_noext + ".wav"], capture_output=True, text=True).stdout
    return float(re.search(r"estimated duration: ([\d.]+)", info).group(1))


def tts(text, path_noext, voice_cfg, engine):
    if engine == "minimax":
        mp3 = path_noext + ".mp3"
        _, chars = llm.minimax_tts(text, mp3, voice_cfg["minimax_voice"], voice_cfg.get("minimax_speed", 1.0))
        dur = _to_wav_m4a(mp3, path_noext)
        os.remove(mp3)
        return dur, chars
    aiff = path_noext + ".aiff"
    subprocess.run(["say", "-v", voice_cfg["voice"].split("（")[0], "-r", str(voice_cfg["rate_wpm"]), "-o", aiff, text], check=True)
    dur = _to_wav_m4a(aiff, path_noext)
    os.remove(aiff)
    return dur, 0


# ---------------- 素材召回 ----------------
def place_conflict(line_places, clip_places):
    """脚本说到的地点和画面能代表的地点对不上，就返回 True。先比城市/地区，脚本只说了国家时再比国家。"""
    if not line_places or not clip_places:
        return False
    ls, cs = set(line_places) - COUNTRIES, set(clip_places) - COUNTRIES
    if ls and cs:
        return not (ls & cs)
    lc, cc = set(line_places) & COUNTRIES, set(clip_places) & COUNTRIES
    if lc and cc:
        return not (lc & cc)
    return False


def retrieve(line, clips, durations):
    need = line.get("tags") or concepts_of(line["en"])
    places = line.get("places") if line.get("places") is not None else places_of(line["en"])
    ranked = []
    # DeepSeek 给的概念按重要性排序，越靠前权重越高；存档脚本的关键词表没有顺序，一视同仁
    weights = [1 / (k + 1) for k in range(len(need))] if line.get("tags") else [1] * len(need)
    for c in clips:
        matched = [x for x in need if x in c["tags"]]
        coverage = sum(w for x, w in zip(need, weights) if x in c["tags"]) / sum(weights) if need else 0
        excluded = None
        if place_conflict(places, c.get("places", [])):
            excluded = f"地点不符：画面是{c['location']}，脚本说的是{'、'.join(places)}"
        loc_hit = 1 if (places and set(places) & set(c.get("places", []))) else 0
        ranked.append({"id": c["id"], "title": c["title"], "coverage": round(coverage, 2), "matched": matched,
                       "quality": c["quality"], "issues": c["issues"], "excluded": excluded, "loc_hit": loc_hit,
                       "clip_s": round(durations[c["id"]], 2)})
    # 旅行视频里"是不是那个地方"很要紧：内容已经对上一半以上、地点也对得上的镜头加 0.4
    boost = lambda r: 0.4 if r["loc_hit"] and r["coverage"] >= 0.5 else 0
    ranked.sort(key=lambda r: (round(r["coverage"] + boost(r), 3), -len(r["issues"]), r["quality"]), reverse=True)
    valid = [r for r in ranked if not r["excluded"] and r["coverage"] > 0]
    bad = [r for r in ranked if r["excluded"] and r["coverage"] > 0]
    # 候选里每个概念至少有一个镜头能覆盖（避免 5 个候选都是同一类画面，后面的镜头补不上没覆盖的概念）
    cands = valid[:5]
    for x in need:
        if valid and x not in valid[0]["matched"] and not any(x in c["matched"] for c in cands):
            extra = next((c for c in valid if x in c["matched"]), None)
            if extra:
                cands.append(extra)
    return need, places, cands, bad[:1]


def wrap(text, n):
    if len(text) <= n:
        return text
    cut = text.rfind(" ", 0, n)
    return text[:cut] + "\n" + text[cut + 1:]


# ---------------- 规则和评分 ----------------
def check_rules(plan, skill):
    first3 = [seg for seg in plan if seg["start"] < 3.0]
    benefit_early = any(re.search(r"\d+%|code|save|off", seg["en"].lower()) for seg in first3)
    shots = [s for seg in plan for s in seg["shots"]]
    faces = [s for s in shots if "faces" in s["issues"]]
    brand = [s for s in shots if "brand" in s["issues"]]
    upto_missing = [seg for seg in plan if "%" in seg["en"] and not re.search(r"up to|from|extra", seg["en"].lower())]
    long_sub = [seg for seg in plan if any(len(x) > skill["subtitle"]["max_chars_per_line"] for x in seg["subtitle"].split("\n"))]
    wrong_place = [s for s in shots if s.get("excluded")]
    n_excl = sum(1 for seg in plan for c in seg["candidates"] if c.get("excluded"))
    return {
        "R1": {"ok": not benefit_early, "msg": "前 3 秒没有价格或优惠信息" if not benefit_early else "前 3 秒出现了利益点"},
        "R2": {"ok": not faces, "msg": "所选镜头里没有可识别人脸（人工标注）" if not faces else f"{len(faces)} 个镜头有可识别人脸"},
        "R3": {"ok": not brand, "msg": "没有第三方品牌标识" if not brand else f"{len(brand)} 个镜头露出手机品牌标识"},
        "R4": {"ok": not upto_missing, "msg": "价格都写了 up to / extra" if not upto_missing else "有价格没写 up to"},
        "R5": {"ok": not long_sub, "msg": "字幕已按 42 字符自动断行" if not long_sub else "有字幕超过 42 字符"},
        "R6": {"ok": not wrong_place, "msg": (f"已排除 {n_excl} 个地点不符的镜头，" if n_excl else "") + "所用镜头地点都和脚本一致" if not wrong_place else "有镜头地点和脚本不符"},
    }


# ---------------- 剪映草稿 ----------------
def write_draft(plan, skill, clips, draft_root, draft_name):
    folder = jy.DraftFolder(draft_root)
    sf = folder.create_draft(draft_name, 1080, 1920, 30, allow_replace=True)
    draft_dir = os.path.join(draft_root, draft_name)
    clip_file = {c["id"]: c["file"] for c in clips}
    mdir = os.path.join(draft_dir, "materials")
    os.makedirs(mdir, exist_ok=True)
    shots = [s for seg in plan for s in seg["shots"]]
    for s in shots:
        dst = os.path.join(mdir, s["clip"] + ".mp4")
        if not os.path.exists(dst):
            shutil.copy(os.path.join(ROOT, clip_file[s["clip"]]), dst)
    for seg in plan:
        shutil.copy(os.path.join(ROOT, seg["audio"] + ".wav"), os.path.join(mdir, os.path.basename(seg["audio"]) + ".wav"))

    sf.append_tracks([jy.TrackSpec(jy.TrackType.video, "画面"), jy.TrackSpec(jy.TrackType.audio, "配音"),
                      jy.TrackSpec(jy.TrackType.text, "字幕"), jy.TrackSpec(jy.TrackType.text, "花字")])
    sub, hz = skill["subtitle"], skill["huazi"]
    for s in shots:
        mat = jy.VideoMaterial(os.path.join(mdir, s["clip"] + ".mp4"))
        src_in = min(s["src_in"], max(0, mat.duration / US - s["dur"]))
        sf.add_segment(jy.VideoSegment(mat, jy.Timerange(int(s["start"] * US), int(s["dur"] * US)),
                                       source_timerange=jy.Timerange(int(src_in * US), int(s["dur"] * US)),
                                       volume=0.0), "画面")
    for seg in plan:
        amat = jy.AudioMaterial(os.path.join(mdir, os.path.basename(seg["audio"]) + ".wav"))
        sf.add_segment(jy.AudioSegment(amat, jy.Timerange(int(seg["start"] * US), amat.duration)), "配音")
        sf.add_segment(jy.TextSegment(seg["subtitle"], jy.Timerange(int(seg["start"] * US), int(seg["dur"] * US)),
                                      style=jy.TextStyle(size=sub["size"], bold=sub["bold"], color=tuple(sub["color"]), align=1),
                                      border=jy.TextBorder(width=sub["border_width"]),
                                      clip_settings=jy.ClipSettings(transform_y=sub["y"])), "字幕")
        for st in seg["stickers"]:
            sf.add_segment(jy.TextSegment(st["text"], jy.Timerange(int(st["start"] * US), int(st["dur"] * US)),
                                          style=jy.TextStyle(size=hz["size"], bold=hz["bold"], color=tuple(hz["color"]), align=1),
                                          border=jy.TextBorder(width=hz["border_width"]),
                                          clip_settings=jy.ClipSettings(transform_y=hz["y"])), "花字")
    sf.save()
    # 剪映 6.0 以后（含 Mac 版 11.x）读的是 draft_info.json；库默认写 draft_content.json（旧版/Windows 叫法），
    # 留着旧名会被新版剪映当成"草稿内容已损坏"。明文写入即可，剪映第一次打开后会自己加密。
    os.replace(os.path.join(draft_dir, "draft_content.json"), os.path.join(draft_dir, "draft_info.json"))
    # 拷一份到输出目录，留作证据
    out_draft = os.path.join(OUT, "jianying_draft")
    if os.path.exists(out_draft):
        shutil.rmtree(out_draft)
    shutil.copytree(draft_dir, os.path.join(out_draft, draft_name))
    return draft_dir, len(shots), sum(len(seg["stickers"]) for seg in plan)


def draft_name_for(topic):
    short = re.sub(r"[\s，,。.！!？?、：:;；/\\\"'“”（）()]+", "", topic)[:12]
    return f"AI粗剪_{short}_{time.strftime('%m%d-%H%M')}"


def clip_meta(clips, durations):
    return {c["id"]: {"title": c["title"], "file": c["file"], "tags": c["tags"], "location": c["location"],
                      "quality": c["quality"], "issues": c["issues"], "pexels": c["pexels"],
                      "clip_s": round(durations[c["id"]], 2)} for c in clips}


def save_result(result):
    with open(os.path.join(OUT, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    with open(os.path.join(OUT, "log.txt"), "w", encoding="utf-8") as f:
        for l in result["log"]:
            f.write(f"[{l['ms']:>6} ms] {l['step']}｜{l['msg']}\n")


# ---------------- 人工换镜头后重写草稿 ----------------
def redraft(swaps_path, draft_root):
    """swaps.json：{"<句子序号>": {"<镜头序号>": "<素材编号>"}}。只换画面，配音和字幕不动。"""
    t0 = time.time()
    result = json.load(open(os.path.join(OUT, "result.json"), encoding="utf-8"))
    swaps = json.load(open(swaps_path, encoding="utf-8"))
    lib, skill = load("library.json"), result["skill"]
    clips = {c["id"]: c for c in lib["clips"]}
    n = 0
    for si, by_shot in swaps.items():
        seg = result["segments"][int(si)]
        for hi, cid in by_shot.items():
            shot = seg["shots"][int(hi)]
            if shot["clip"] == cid:
                continue
            cand = next((c for c in seg["candidates"] if c["id"] == cid), None)
            c = clips[cid]
            shot.update({"clip": cid, "title": c["title"], "quality": c["quality"], "issues": c["issues"],
                         "coverage": cand["coverage"] if cand else 0, "excluded": cand["excluded"] if cand else None})
            n += 1
    hits = check_rules(result["segments"], skill)
    for r in result["rules"]:
        r.update(hits[r["id"]])
    result["run_id"] = time.strftime("%Y%m%d%H%M%S")
    # 写回原来那个草稿所在的文件夹（同名覆盖），不另开新草稿
    draft_root = os.path.dirname(result["draft"]["path"]) or draft_root
    draft_dir, n_shots, n_st = write_draft(result["segments"], skill, lib["clips"], draft_root, result["draft"]["name"])
    result["score"] = review_draft(draft_dir, skill)
    result["log"].append({"step": "人工换镜头", "msg": f"换了 {n} 个镜头，重新写入剪映草稿「{result['draft']['name']}」，已重新检查草稿",
                          "ms": result["log"][-1]["ms"] + int((time.time() - t0) * 1000)})
    save_result(result)
    print(f"redraft: {n} swaps, draft checked")


def generate_script_session(topic, market, platform, brief=""):
    """Only write scripts; keep the existing preview, audio and draft intact."""
    import uuid
    facts, lib = load("facts.json"), load("library.json")
    raw, usage, _, _ = write_scripts_llm(topic, market, platform, facts, lib, brief)
    variants = []
    for v in raw:
        checks = check_script(v, facts)
        variants.append({**v, "checks": checks, "ok": all(c["ok"] for c in checks)})
    if not variants:
        raise ValueError("模型未返回脚本，请重试。")
    return {"id": uuid.uuid4().hex, "title": topic, "country": market, "platform": platform,
            "usage": usage, "scripts": {"variants": variants, "chosen": None, "facts": facts["facts"],
            "generated_by": f"{usage['provider']} · {usage['model']} · {time.strftime('%Y-%m-%d %H:%M')}"}}


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft-root", default=DEFAULT_DRAFT_ROOT)
    ap.add_argument("--topic", help="输入选题（中文），用 DeepSeek 现写脚本")
    ap.add_argument("--market", default="英国")
    ap.add_argument("--platform", default="TikTok")
    ap.add_argument("--tts", choices=["auto", "minimax", "macos"], default="auto")
    ap.add_argument("--script-file", help="使用已生成并由用户选定的脚本")
    ap.add_argument("--variant", help="用户选定的脚本版本")
    ap.add_argument("--redraft", help="人工换镜头的 swaps.json")
    args = ap.parse_args()
    if args.redraft:
        return redraft(args.redraft, args.draft_root)

    prepared = None
    if args.script_file:
        with open(args.script_file, encoding="utf-8") as f:
            prepared = json.load(f)
        if args.variant not in [v["id"] for v in prepared["scripts"]["variants"]]:
            raise ValueError("请选择有效的脚本版本")
        args.topic, args.market, args.platform = prepared["title"], prepared["country"], prepared["platform"]
    t0 = time.time()
    tts_dir = os.path.join(OUT, "tts")
    shutil.rmtree(tts_dir, ignore_errors=True)
    os.makedirs(tts_dir, exist_ok=True)

    script, lib, skill, rules = load("script.json"), load("library.json"), load("skill.json"), load("rules.json")
    topics, facts, cands = load("topics.json"), load("facts.json"), load("script_candidates.json")
    clips = lib["clips"]
    usage_info = {}

    # 选题
    topics = with_review_rates(topics)   # 有复盘结果就用复盘出来的千播率，没有才用示例值
    scored = select_topic(topics)
    if args.topic:
        topic_title = args.topic.strip()
        script.update({"title": topic_title, "country": args.market, "platform": args.platform})
        topic_out = {"note": topics["note"], "month": topics["month"], "nodes": topics["nodes"], "pool": scored,
                     "chosen": "INPUT", "input": topic_title}
        log("选题", f"人工输入选题「{topic_title}」（{args.market} · {args.platform}），跳过选题池打分", t0)
    else:
        pick = scored[0]
        topic_title = pick["title"]
        topic_out = {"note": topics["note"], "month": topics["month"], "nodes": topics["nodes"], "pool": scored, "chosen": pick["id"]}
        log("选题", f"{len(scored)} 个候选选题，按 历史爆款率×0.5 + 营销节点×0.3 + 利益点可植入×0.2 打分，选中 {pick['id']}「{pick['title']}」（{pick['score']}）", t0)

    # 写脚本，或复用已经确认的版本（不再次调用模型）
    if prepared:
        raw, usage = prepared["scripts"]["variants"], prepared["usage"]
        usage_info["deepseek"] = usage
        generated_by = prepared["scripts"]["generated_by"]
    elif args.topic:
        raw, usage, sys_prompt, user_prompt = write_scripts_llm(topic_title, args.market, args.platform, facts, lib)
        usage_info["deepseek"] = usage
        generated_by = f"{usage['provider']}（{usage['model']}，{time.strftime('%Y-%m-%d %H:%M')} 现场生成，{usage.get('total_tokens', '?')} tokens，{usage.get('seconds')} 秒）"
    else:
        raw, generated_by = cands["variants"], cands["generated_by"]
    variants = []
    for v in raw:
        ch = check_script(v, facts)
        variants.append({"id": v["id"], "idea": v.get("idea"), "lines": v["lines"], "claims": v.get("claims", []),
                         "checks": ch, "ok": all(c["ok"] for c in ch)})
    passed = [v for v in variants if v["ok"]]
    chosen = passed[0] if passed else min(variants, key=lambda v: sum(not c["ok"] for c in v["checks"]))
    if prepared:
        chosen = next(v for v in variants if v["id"] == args.variant)
    rejected = [f"{v['id']} 版：{next(c['msg'] for c in v['checks'] if not c['ok'])}" for v in variants if not v["ok"]]
    script["lines"] = [dict(l) for l in chosen["lines"]]
    who = usage_info["deepseek"]["provider"] if args.topic else "脚本 Agent"
    models = llm.models_in_use() if args.topic else {}   # 存档示例不调模型
    if prepared:
        log("写脚本", f"复用已保存脚本 v{prepared.get('revision', 1)} · {chosen['id']} 版，不再次调用脚本模型" + ("" if chosen["ok"] else "（仍有规则问题，需人工核对）"), t0)
    else:
        log("写脚本", f"{who} 出 {len(variants)} 版初稿，审核拦下 {len(rejected)} 版，采用 {chosen['id']} 版" + ("" if passed else "（所有版本均有问题，需人工修改）"), t0)
    log("读取 Skill", f"{skill['name']}：{len(skill.get('rules', []))} 条规则，单镜头 ≤ {skill['max_shot_s']} 秒、字幕/花字样式、利益点和结尾钩子", t0)

    durations = {c["id"]: jy.VideoMaterial(os.path.join(ROOT, c["file"])).duration / US for c in clips}
    log("读取素材库", f"{len(clips)} 段实拍，已打标签（地点、场景、清晰度、风险）", t0)

    # 配音
    voice = skill["voice"]
    engine = args.tts if args.tts != "auto" else ("minimax" if llm.has("MINIMAX_API_KEY") else "macos")
    lines = script["lines"]
    chars = 0
    fallback_note = ""
    for i, ln in enumerate(lines):
        ln["audio"] = f"output/tts/line{i + 1}"
        try:
            d, c = tts(ln["en"], os.path.join(ROOT, ln["audio"]), voice, engine)
        except Exception as e:  # MiniMax 出错（余额不足、限流等）就整条改用系统语音，保证一条视频里声音一致
            if engine != "minimax":
                raise
            msg = str(e)
            fallback_note = "MiniMax 余额不足" if "insufficient balance" in msg else f"MiniMax 出错：{msg[:60]}"
            engine, chars = "macos", 0
            for j in range(i + 1):
                lj = lines[j]
                lj["audio"] = f"output/tts/line{j + 1}"
                dj, _ = tts(lj["en"], os.path.join(ROOT, lj["audio"]), voice, engine)
                lj["voice_s"] = round(dj, 2)
            continue
        ln["voice_s"], chars = round(d, 2), chars + c
    if engine == "minimax":
        usage_info["minimax"] = {"characters": chars, "voice": voice["minimax_voice"]}
    voice_name = f"MiniMax {voice['minimax_voice']}" if engine == "minimax" else voice["voice"]
    if models and engine != "minimax":   # 配音实际退回了系统语音，如实记录
        models["tts"] = {"provider": "macOS 系统语音", "model": voice["voice"]}
    log("生成配音", f"{len(lines)} 句，共 {sum(l['voice_s'] for l in lines):.1f} 秒（{voice_name}）" + (f"；{fallback_note}，改用系统语音" if fallback_note else ""), t0)

    # 分段：每句一段，段与段之间留气口
    t = 0.0
    for i, ln in enumerate(lines):
        ln["start"] = round(t, 3)
        gap = skill["gap_between_lines_s"] if i < len(lines) - 1 else skill["tail_s"]
        ln["seg_s"] = round(ln["voice_s"] + gap, 3)
        t += ln["seg_s"]
    total_s = round(t, 2)
    log("按语义和气口分段", f"{len(lines)} 段，总时长 {total_s} 秒", t0)

    # 素材召回 + 地点规则
    plan = []
    n_excluded = 0
    for i, ln in enumerate(lines):
        need, places, top, bad = retrieve(ln, clips, durations)
        n_excluded += len(bad)
        seg = {"i": i, "en": ln["en"], "zh": ln.get("zh", ""), "role": ln.get("role", ""), "start": ln["start"], "dur": ln["seg_s"],
               "voice_s": ln["voice_s"], "audio": ln["audio"], "need": need, "places": places,
               "candidates": top + bad, "shots": [], "stickers": [], "gap": None, "subtitle": wrap(ln["en"], skill["subtitle"]["max_chars_per_line"])}
        if not top:
            seg["gap"] = f"素材缺口：素材库里没有和「{'、'.join(need) or '这句'}」相关的镜头，先用空镜占位，需要补拍或换素材"
            fallback = sorted([c for c in clips if not place_conflict(places, c.get("places", []))], key=lambda c: -c["quality"])[0]
            top = [{"id": fallback["id"], "title": fallback["title"], "coverage": 0, "matched": [], "quality": fallback["quality"],
                    "issues": fallback["issues"], "excluded": None, "loc_hit": 0, "clip_s": round(durations[fallback["id"]], 2)}]
            seg["candidates"] = top + bad
        # 单镜头太长就切成多个镜头（Skill 规则：每个镜头不超过 max_shot_s），后面的镜头优先补上前面没覆盖到的概念
        n_need = min(3, max(1, -(-seg["dur"] // skill["max_shot_s"])))
        n_need = int(n_need)
        picked = [top[0]]
        covered = set(top[0]["matched"])
        for cand in top[1:]:
            if len(picked) >= n_need:
                break
            if set(cand["matched"]) - covered:
                picked.append(cand)
                covered |= set(cand["matched"])
        for cand in top[1:]:
            if len(picked) >= n_need:
                break
            if cand not in picked and not cand["issues"]:
                picked.append(cand)
        if len(picked) < n_need and not seg["gap"]:
            seg["gap"] = f"素材缺口：和「{'、'.join(need)}」相关的镜头不够，这句只能用 {len(picked)} 个镜头撑 {seg['dur']:.1f} 秒"
        each = round(seg["dur"] / len(picked), 3)
        parts = [(c, seg["start"] + k * each, each if k < len(picked) - 1 else round(seg["dur"] - each * (len(picked) - 1), 3))
                 for k, c in enumerate(picked)]
        for cand, st, d in parts:
            src_in = min(skill["clip_skip_head_s"], max(0, durations[cand["id"]] - d))
            seg["shots"].append({"clip": cand["id"], "title": cand["title"], "start": round(st, 3), "dur": round(d, 3),
                                 "src_in": round(src_in, 2), "coverage": cand["coverage"], "quality": cand["quality"],
                                 "issues": cand["issues"], "excluded": cand["excluded"]})
        plan.append(seg)
    log("本地素材库召回", f"每句按画面概念召回 5 个候选；排除地点不符 {n_excluded} 个；{sum(len(s['shots']) for s in plan)} 个镜头", t0)

    # 花字 / 贴纸
    for seg, ln in zip(plan, lines):
        if not ln.get("on_screen"):
            continue
        if seg["i"] == 0:
            st, d, kind = 0.0, min(skill["opening_huazi_max_s"], seg["dur"]), "开头花字"
        elif seg["i"] == len(plan) - 1:
            st, d, kind = seg["start"] + 0.2, seg["dur"] - 0.2, "结尾钩子"
        else:
            st, d, kind = seg["start"] + skill["benefit_sticker_offset_s"], skill["benefit_sticker_len_s"], "利益点贴纸"
        seg["stickers"].append({"kind": kind, "text": ln["on_screen"], "start": round(st, 3), "dur": round(d, 3)})

    # 规则检查（Demo 规则库：英国 × TikTok）
    hits = check_rules(plan, skill)
    rule_out = [{"id": r["id"], "name": r["name"], "why": r["why"], **hits[r["id"]]} for r in rules["rules"]]
    log("检索国家 / 平台规则", f"{script['country']} × {script['platform']}，命中 {len(rule_out)} 条，通过 {sum(r['ok'] for r in rule_out)} 条", t0)

    # 剪映草稿
    draft_name = draft_name_for(topic_title) if args.topic else DEMO_DRAFT_NAME
    draft_dir, n_shots, n_st = write_draft(plan, skill, clips, args.draft_root, draft_name)
    log("编排剪映轨道", f"已生成剪映草稿「{draft_name}」：画面 {n_shots} 段、配音 {len(plan)} 段、字幕 {len(plan)} 段、花字 {n_st} 个", t0)

    review = review_draft(draft_dir, skill)
    log("草稿检查", f"完成 {len(review['checks'])} 项检查；画面内容等仍需人工核验", t0)

    result = {"mode": "live" if args.topic else "demo", "run_id": time.strftime("%Y%m%d%H%M%S"),
              "topic": topic_out,
              "scripts": {"session_id": prepared["id"] if prepared else None, "generated_by": generated_by, "variants": variants, "chosen": chosen["id"], "rejected": rejected, "facts": facts["facts"]},
              "title": script["title"] if args.topic else script["title"], "country": script["country"], "platform": script["platform"],
              "skill": {**skill, "voice": {**voice, "used": voice_name, "engine": engine}}, "total_s": total_s, "segments": plan,
              "rules": rule_out, "score": review,
              "log": log_lines, "draft": {"name": draft_name, "path": draft_dir, "shots": n_shots, "stickers": n_st},
              "usage": usage_info, "models": models or None, "clips": clip_meta(clips, durations)}
    save_result(result)
    print("done:", os.path.join(OUT, "result.json"))


if __name__ == "__main__":
    main()
