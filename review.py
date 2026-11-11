"""数据复盘：把播放数对回每条作品，出作品日报，标出千播，再把复盘结果回写到选题打分。

做的是第六节（数据自动统计和复盘）里最小的一段：
  发布记录 + 每天的取数表 → 作品日报（千播、待补、零播）→ 题材表现 → 回写选题池 → 一条复测建议。
对账、去重、千播标记都是固定规则；只有"两条样片差在哪"交给模型看。

用法：
  .venv/bin/python review.py              # 导入 data/review/ 里的取数表，写出 data/review/review.json
  .venv/bin/python review.py --no-ai      # 不调模型，不出复测建议

发布记录和播放数是模拟的（data/review/），规则和计算是真的。
"""
import argparse
import csv
import glob
import json
import os
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data", "review")
HIT_VIEWS = 1000      # 业务方的说法：新号有一条到千播，才算"比较有潜力"
MIN_SAMPLES = 3       # 一个题材不到 3 条作品，样本太少，不单独算


def key(row):
    return f"{row['work_id']}@{row['platform']}"


def apply_report(state, rows, published):
    """导入一张取数表。state 是 {作品@平台: 读数}，原地更新；返回这张表里对不上发布记录的行。

    - 同一条作品重复导入，只更新读数，不会变成两行。
    - 取数失败（status 不是 ok，或者没有数字）：保留上次的读数和时间，标成待补。
    - 平台返回 0 就记 0，和取数失败是两回事。
    """
    known = {key(w) for w in published}
    unmatched = []
    for row in rows:
        k = key(row)
        if k not in known:
            unmatched.append(dict(row))
            continue
        cur = state.setdefault(k, {"views": None, "fetched_at": None, "prev_views": None, "prev_fetched_at": None,
                                   "pending": False, "hit_at": None})
        raw = str(row.get("views", "")).strip()
        if row.get("status", "ok") != "ok" or not raw.isdigit():
            cur["pending"] = True
            cur["pending_since"] = row["fetched_at"]
            continue
        views, at = int(raw), row["fetched_at"]
        if cur["fetched_at"] is not None and at < cur["fetched_at"]:
            continue                                   # 比手上的读数还旧，不要
        if cur["fetched_at"] is not None and at > cur["fetched_at"]:
            cur["prev_views"], cur["prev_fetched_at"] = cur["views"], cur["fetched_at"]
        cur.update(views=views, fetched_at=at, pending=False)
        if views >= HIT_VIEWS and cur["hit_at"] is None:
            cur["hit_at"] = at                         # 第一次过千的那次取数
    return unmatched


def daily_rows(state, published):
    """作品日报：一行对应一个平台上的一条作品。"""
    latest = max((s["fetched_at"] for s in state.values() if s["fetched_at"]), default=None)
    rows = []
    for work in published:
        s = state.get(key(work))
        row = {k: work[k] for k in ("work_id", "platform", "account", "script_id", "title", "genre")}
        if s is None or s["views"] is None:
            row.update(views=None, delta=None, fetched_at=None, tags=["待补"], note="还没取到过数")
        else:
            tags = []
            if s["views"] >= HIT_VIEWS:
                tags.append("值得继续测")
            if s["pending"]:
                tags.append("待补")
            if s["views"] == 0 and not s["pending"]:
                tags.append("零播")
            row.update(views=s["views"], fetched_at=s["fetched_at"], tags=tags,
                       delta=None if s["prev_views"] is None else s["views"] - s["prev_views"],
                       new_hit=bool(s["hit_at"] and s["hit_at"] == latest),
                       note=f"这次没取到，保留 {s['fetched_at']} 的读数" if s["pending"] else "")
        rows.append(row)
    return rows


def genre_rates(rows):
    """按题材算千播率，用来替换选题池里的示例爆款率。

    千播率 =（千播作品数 + 1）÷（作品数 + 2）。加一平滑是为了样本少时不出现 0% 或 100%。
    题材不到 3 条作品时，用全部作品的千播率代替。
    """
    counted = [r for r in rows if r["views"] is not None]
    smooth = lambda hits, n: round((hits + 1) / (n + 2), 3)
    overall = smooth(sum(r["views"] >= HIT_VIEWS for r in counted), len(counted))
    out = {}
    for genre in sorted({r["genre"] for r in rows}):
        mine = [r for r in counted if r["genre"] == genre]
        hits = sum(r["views"] >= HIT_VIEWS for r in mine)
        enough = len(mine) >= MIN_SAMPLES
        out[genre] = {"works": len(mine), "hits": hits, "rate": smooth(hits, len(mine)) if enough else overall,
                      "note": "" if enough else f"只有 {len(mine)} 条，样本太少，用全部作品的千播率代替"}
    return out, overall


def pick_pair(rows):
    """找一组可比的样片：同题材、同平台，一条过千、一条最低。找不到就不出复测建议。"""
    for high in sorted((r for r in rows if r["views"] is not None and r["views"] >= HIT_VIEWS), key=lambda r: -r["views"]):
        lows = [r for r in rows if r["genre"] == high["genre"] and r["platform"] == high["platform"]
                and r["script_id"] != high["script_id"] and r["views"] is not None and r["views"] < HIT_VIEWS and "待补" not in r["tags"]]
        if lows:
            return high, min(lows, key=lambda r: r["views"])
    return None


def retest_suggestion(high, low, scripts, ask):
    """让模型对照两条脚本，只说看得见的差异，再给一项下周能试的改动。运营复核，组长决定。"""
    system = ("你是短视频运营助手。下面是同题材、同平台、同一天发布的两条英文口播脚本，一条播放过千，一条没过。"
              "只对照文本，说出一项能直接观察到的差异，不要下'因为这个所以播放高'的因果结论。"
              "再给一项下周可以试的改动（只改一处），和要看的指标。改动是给'播放没过千'的那条用的，举例要符合它自己的选题。全部用中文，简短。"
              '只输出 JSON：{"difference":"","change":"","metric":""}')
    user = json.dumps({"播放过千": {"标题": high["title"], "播放": high["views"], "脚本": scripts.get(high["script_id"], [])},
                       "播放没过千": {"标题": low["title"], "播放": low["views"], "脚本": scripts.get(low["script_id"], [])}}, ensure_ascii=False)
    data, usage = ask(system, user)
    fields = {k: str(data.get(k, "")).strip() for k in ("difference", "change", "metric")}
    if not all(fields.values()):
        raise ValueError("模型没有给全三项")
    return {**fields, "model": f"{usage.get('provider', '')} · {usage.get('model', '')}".strip(" ·")}


def topic_scores(rates):
    """用给定的题材爆款率给选题池打分（和 pipeline.select_topic 是同一个公式）。"""
    import pipeline
    topics = pipeline.load("topics.json")
    return pipeline.select_topic(dict(topics, genre_hit_rate=rates))


def build(use_ai=True):
    published_file = json.load(open(os.path.join(DATA, "published.json"), encoding="utf-8"))
    published = published_file["works"]
    state, unmatched, reports = {}, [], []
    for path in sorted(glob.glob(os.path.join(DATA, "report_*.csv"))):
        with open(path, encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        unmatched += [dict(r, report=os.path.basename(path)) for r in apply_report(state, rows, published)]
        reports.append({"file": os.path.basename(path), "rows": len(rows)})
    rows = daily_rows(state, published)
    genres, overall = genre_rates(rows)
    import pipeline
    example = pipeline.load("topics.json")["genre_hit_rate"]
    pick = lambda scored: [{"id": t["id"], "title": t["title"], "genre": t["genre"], "score": t["score"]} for t in scored]
    review = {
        "note": published_file["note"], "generated_at": time.strftime("%Y-%m-%d %H:%M"), "hit_views": HIT_VIEWS,
        "reports": reports, "latest": max((r["fetched_at"] for r in rows if r["fetched_at"]), default=None),
        "rows": rows, "unmatched": unmatched,
        "genres": genres, "overall_rate": overall, "genre_hit_rate": {g: v["rate"] for g, v in genres.items()},
        "topics_before": pick(topic_scores(example)), "topics_after": pick(topic_scores({g: v["rate"] for g, v in genres.items()})),
        "retest": None,
    }
    pair = pick_pair(rows)
    if pair:
        high, low = pair
        review["retest"] = {"high": {k: high[k] for k in ("work_id", "title", "views", "platform")},
                            "low": {k: low[k] for k in ("work_id", "title", "views", "platform")}}
        scripts = {w["script_id"]: w["lines"] for w in published}
        if use_ai:
            try:
                import llm
                review["retest"].update(retest_suggestion(high, low, scripts, lambda s, u: llm.script_json(s, u, temperature=0.3)))
            except Exception as e:   # 没 key 或接口出错：日报照出，只是没有复测建议
                review["retest"]["error"] = f"没有生成复测建议：{str(e)[:80]}"
    with open(os.path.join(DATA, "review.json"), "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=1)
        f.write("\n")
    return review


def main():
    ap = argparse.ArgumentParser(description="数据复盘")
    ap.add_argument("--no-ai", action="store_true", help="不调模型，不出复测建议")
    r = build(use_ai=not ap.parse_args().no_ai)
    print(f"作品日报（{r['latest']}，导入 {len(r['reports'])} 张取数表）")
    for row in r["rows"]:
        delta = "" if row["delta"] is None else f"（{row['delta']:+d}）"
        print(f"  {row['work_id']} {row['platform']:<9} {row['genre']:<4} {str(row['views']):>5}{delta:<8} {'、'.join(row['tags'])} {row['note']}")
    print("对不上发布记录的行：", [f"{u['work_id']}@{u['platform']}" for u in r["unmatched"]] or "无")
    print("题材千播率：", {g: f"{v['rate']}（{v['hits']}/{v['works']}）" for g, v in r["genres"].items()})
    print("选题得分：", [(a["id"], b["score"], "→", a["score"]) for a in r["topics_after"] for b in r["topics_before"] if a["id"] == b["id"]])
    if r["retest"]:
        print("复测建议：", {k: v for k, v in r["retest"].items() if k not in ("high", "low")})


if __name__ == "__main__":
    main()
