"""检查实际写入的剪映草稿；不推断成片内容或主观质量。"""
import json
import os


def review_draft(draft_dir, skill):
    result = {
        "subject": "已生成的剪映草稿：检查轨道信息和引用文件，尚未检查导出的视频",
        "checks": [],
        "pending": ["画面与脚本是否真正匹配", "配音内容、音量和字幕是否准确", "画质、节奏及观看体验", "人脸、品牌、事实和平台规则需人工核验", "最终导出视频是否正常播放"],
    }
    checks = result["checks"]
    def add(name, status, evidence):
        checks.append({"name": name, "status": status, "evidence": evidence})
    try:
        with open(os.path.join(draft_dir, "draft_info.json"), encoding="utf-8") as f:
            draft = json.load(f)
        tracks = draft["tracks"]
        materials = draft["materials"]
        if not isinstance(tracks, list) or not isinstance(materials, dict):
            raise ValueError("轨道或素材数据格式不正确")
    except (OSError, ValueError, KeyError, TypeError) as e:
        add("读取草稿", "unknown", f"无法读取草稿：{e}")
        return result
    add("读取草稿", "pass", "已读取生成的 draft_info.json。")
    try:
        selected = {name: next((t for t in tracks if t.get("name") == name), None) for name in ("画面", "配音", "字幕")}
        segs = {name: t.get("segments", []) if t else [] for name, t in selected.items()}
        for name in segs:
            if not isinstance(segs[name], list):
                raise ValueError(f"{name}轨道格式不正确")
        all_media = {m["id"]: m for kind in ("videos", "audios") for m in materials.get(kind, [])}
        refs = [s.get("material_id") for name in ("画面", "配音") for s in segs[name]]
        missing_ref = [ref for ref in refs if ref not in all_media]
        missing_files = []
        for ref in refs:
            m = all_media.get(ref)
            if m:
                path = m.get("path", "")
                if not path or not os.path.isfile(path) or os.path.getsize(path) == 0:
                    missing_files.append(os.path.basename(path) or str(ref))
        if not refs:
            add("画面和配音文件", "fail", "草稿没有画面或配音片段。")
        elif missing_ref or missing_files:
            add("画面和配音文件", "fail", f"{len(missing_ref)} 个素材引用无法找到，{len(missing_files)} 个文件缺失或为空。")
        else:
            add("画面和配音文件", "pass", f"{len(refs)} 个画面和配音片段的引用文件均存在且非空；未检查文件能否解码。")
        def intervals(name):
            out = []
            for s in segs[name]:
                r = s["target_timerange"]
                start, duration = r["start"], r["duration"]
                if not isinstance(start, (int, float)) or not isinstance(duration, (int, float)) or start < 0 or duration <= 0:
                    raise ValueError(f"{name}时间范围无效")
                out.append((start, start + duration))
            return sorted(out)
        video, audio, subtitle = (intervals(n) for n in ("画面", "配音", "字幕"))
        def uncovered(targets, covers):
            missing = 0
            for start, end in targets:
                cursor = start
                for a, b in covers:
                    if b <= cursor: continue
                    if a > cursor + 1000: break
                    cursor = max(cursor, b)
                    if cursor >= end - 1000: break
                if cursor < end - 1000: missing += 1
            return missing
        if not audio:
            add("配音覆盖", "fail", "草稿没有配音片段。")
        else:
            for label, cover in (("配音期间画面覆盖", video), ("配音期间字幕覆盖", subtitle)):
                n = uncovered(audio, cover)
                add(label, "fail" if n else "pass", f"{len(audio)} 段配音中有 {n} 段未被对应轨道完整覆盖（允许 1 毫秒误差）。仅检查时间范围。")
        if not video:
            add("镜头时长", "fail", "草稿没有画面片段。")
        else:
            limit = skill.get("max_shot_s")
            if not isinstance(limit, (int, float)) or limit <= 0:
                add("镜头时长", "unknown", "缺少有效的镜头时长规则。")
            else:
                long = sum((end - start) / 1_000_000 > limit + 0.05 for start, end in video)
                add("镜头时长", "fail" if long else "pass", f"{len(video)} 个画面片段中，{long} 个超过 {limit} 秒（允许 0.05 秒误差）。")
    except (KeyError, TypeError, ValueError, OSError) as e:
        add("轨道检查", "unknown", f"无法完整检查轨道：{e}")
    return result
