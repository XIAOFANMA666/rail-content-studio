"""从成片里量出剪辑规则（蒸馏剪辑 Skill 的第一步）。

给它一批表现好的成片，它量出能量的东西：镜头多长、多久切一次、语速多快、每句几个词、
开头 3 秒有没有提价格、结尾有没有行动引导。结果和现在的 Skill 设定值并排列出来，
人看过以后再决定改不改 data/skill.json。

用法：
  .venv/bin/python tools/distill_skill.py 成片1.mp4 成片2.mp4 ...
  .venv/bin/python tools/distill_skill.py --write 成片*.mp4     # 同时把实测结果记进 data/skill.json 的 measured

量不了的（要人看或要看图模型）：字幕样式、花字设计、选什么画面。
"""
import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SAMPLE_FPS = 15          # 找切点时每秒看几帧
CUT_FLOOR = 8            # 相邻两帧的平均亮度差（0–255）至少这么大才可能是切镜头
CUT_RATIO = 4            # 而且要比前后几帧的变化大这么多倍：镜头自己在晃的时候不算切
MIN_SHOT_S = 0.4         # 比这还短的"镜头"多半是闪白或转场，并进前一个
PRICE_WORDS = re.compile(r"(\d+\s?%|£|€|\$|\bsave\b|\bdiscount\b|\boff\b|\bcheap|\bfree\b|\bcode\b|\bdeal\b)", re.I)
CTA_WORDS = re.compile(r"\b(search|download|tap|click|book|use code|link|app)\b", re.I)


def shot_lengths(cuts, duration):
    """切点（秒）→ 每个镜头的时长。太近的切点并掉。"""
    points = [0.0]
    for cut in sorted(cuts):
        if cut - points[-1] >= MIN_SHOT_S and duration - cut >= MIN_SHOT_S:
            points.append(cut)
    points.append(duration)
    return [round(b - a, 2) for a, b in zip(points, points[1:])]


def percentile(values, q):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(q * (len(ordered) - 1))))]


def speech_stats(segments, duration):
    """口播句段 → 语速、每句词数、句间停顿、开头有没有提价格、结尾有没有行动引导。"""
    words = [len(re.findall(r"[A-Za-z0-9']+", s["text"])) for s in segments]
    spoken = sum(s["end"] - s["start"] for s in segments)
    opening = " ".join(s["text"] for s in segments if s["start"] < 3.0)
    last = segments[-1] if segments else None
    return {
        "sentences": len(segments),
        "words_per_minute": round(sum(words) / spoken * 60) if spoken > 0 else None,
        "max_words_per_sentence": max(words) if words else None,
        "price_in_first_3s": bool(PRICE_WORDS.search(opening)),
        "cta_at_end": bool(last and CTA_WORDS.search(last["text"])),
        "cta_seconds_before_end": round(duration - last["start"], 1) if last else None,
    }


def summarize(videos):
    """多条成片的实测 → 一组建议值。镜头时长把所有成片的镜头放在一起算。"""
    shots = [d for v in videos for d in v["shots"]]
    rates = [v["speech"]["words_per_minute"] for v in videos if v["speech"]["words_per_minute"]]
    return {
        "videos": len(videos),
        "shots": len(shots),
        "shot_median_s": round(statistics.median(shots), 2) if shots else None,
        "shot_p90_s": percentile(shots, 0.9) if shots else None,
        "shot_max_s": max(shots) if shots else None,
        "words_per_minute": round(statistics.median(rates)) if rates else None,
        "max_words_per_sentence": max((v["speech"]["max_words_per_sentence"] or 0) for v in videos) if videos else None,
        "price_in_first_3s": sum(v["speech"]["price_in_first_3s"] for v in videos),
        "cta_at_end": sum(v["speech"]["cta_at_end"] for v in videos),
        "duration_median_s": round(statistics.median(v["duration"] for v in videos), 1) if videos else None,
    }


def compare(summary, skill):
    """实测值和 Skill 设定值并排，给出一句判断。返回 [(项目, 设定, 实测, 判断)]。"""
    rows = []
    limit = skill.get("max_shot_s")
    if summary["shot_max_s"] is not None and limit:
        verdict = "一致" if summary["shot_p90_s"] <= limit else f"成片里 90% 的镜头在 {summary['shot_p90_s']} 秒以内，设定值偏紧"
        rows.append(("单个镜头最长", f"{limit} 秒", f"中位 {summary['shot_median_s']} 秒，90% 在 {summary['shot_p90_s']} 秒内，最长 {summary['shot_max_s']} 秒", verdict))
    low, high = (skill.get("voice") or {}).get("wpm_range") or (None, None)
    if summary["words_per_minute"] and low:
        rate = summary["words_per_minute"]
        rows.append(("语速", f"{low}–{high} 词/分钟", f"{rate} 词/分钟", "一致" if low <= rate <= high else "实测偏慢" if rate < low else "实测偏快"))
    if summary["max_words_per_sentence"]:
        rows.append(("每句最多几个词", "14 个", f"{summary['max_words_per_sentence']} 个", "一致" if summary["max_words_per_sentence"] <= 14 else "成片里有更长的句子"))
    rows.append(("开头 3 秒不提价格", "不提", f"{summary['videos'] - summary['price_in_first_3s']} / {summary['videos']} 条没提", "一致" if not summary["price_in_first_3s"] else "有成片开头就提了价格"))
    rows.append(("结尾有行动引导", "有", f"{summary['cta_at_end']} / {summary['videos']} 条有", "一致" if summary["cta_at_end"] == summary["videos"] else "有成片结尾没有引导"))
    return rows


# ---------- 下面是要跑 ffmpeg 和语音识别的部分 ----------

def find_cuts(diffs, fps=SAMPLE_FPS, window=6):
    """相邻帧的差值序列 → 切点（秒）。差值突然比前后都大很多，才算切镜头。"""
    cuts = []
    for i, value in enumerate(diffs):
        near = sorted(list(diffs[max(0, i - window):i]) + list(diffs[i + 1:i + 1 + window]))
        typical = max(near[len(near) // 2], 0.5) if near else 0.5
        if value >= CUT_FLOOR and value >= CUT_RATIO * typical:
            cuts.append(round((i + 1) / fps, 2))
    return cuts


def detect_cuts(path):
    """把每帧缩成 48×48 的灰度图，比相邻两帧。返回 (切点, 视频时长)。

    试过 ffmpeg 自带的场景检测，两个画面亮度接近时会漏切点；这个办法在两条已知切点的视频上 15 个切点都找到了，没有多报。
    """
    import imageio_ffmpeg
    import numpy as np
    side = 48
    raw = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-v", "error", "-nostdin", "-i", path, "-an",
                          "-vf", f"fps={SAMPLE_FPS},scale={side}:{side},format=gray", "-f", "rawvideo", "pipe:1"],
                         capture_output=True, timeout=600, check=True).stdout
    frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, side * side).astype(np.int16)
    diffs = np.abs(frames[1:] - frames[:-1]).mean(axis=1).tolist()
    return find_cuts(diffs), len(frames) / SAMPLE_FPS


def measure(path):
    import video_review
    cuts, duration = detect_cuts(path)
    with tempfile.TemporaryDirectory(prefix="distill-") as tmp:
        audio = os.path.join(tmp, "audio.mp3")
        video_review._ffmpeg(["-i", path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", audio])
        segments = video_review._transcribe(audio)
    return {"file": os.path.basename(path), "duration": round(duration, 2),
            "shots": shot_lengths(cuts, duration), "speech": speech_stats(segments, duration)}


def main():
    ap = argparse.ArgumentParser(description="从成片里量出剪辑规则")
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--write", action="store_true", help="把实测结果记进 data/skill.json 的 measured")
    ap.add_argument("--note", default="", help="这批成片是什么（写进 measured.source）")
    args = ap.parse_args()
    videos = [measure(p) for p in args.videos]
    summary = summarize(videos)
    skill_path = os.path.join(ROOT, "data", "skill.json")
    skill = json.load(open(skill_path, encoding="utf-8"))
    rows = compare(summary, skill)
    for v in videos:
        print(f"{v['file']}：{v['duration']} 秒，{len(v['shots'])} 个镜头 {v['shots']}，{v['speech']}")
    print(f"\n{'项目':<10}｜{'Skill 设定':<12}｜实测｜判断")
    for name, setting, measured, verdict in rows:
        print(f"{name}｜{setting}｜{measured}｜{verdict}")
    if args.write:
        skill["measured"] = {"source": args.note or "、".join(v["file"] for v in videos), **summary,
                             "compare": [dict(zip(("item", "setting", "measured", "verdict"), r)) for r in rows]}
        with open(skill_path, "w", encoding="utf-8") as f:
            json.dump(skill, f, ensure_ascii=False, indent=1)
            f.write("\n")
        print(f"\n已写入 {skill_path} 的 measured")


if __name__ == "__main__":
    main()
