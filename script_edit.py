"""Validate edited scripts and draft AI rewrites using the configured model."""
import json
from pathlib import Path

import llm

DATA = Path(__file__).parent / "data"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def vocabulary():
    clips = load("library.json")["clips"]
    return {"tags": sorted({t for c in clips for t in c["tags"]}),
            "places": sorted({p for c in clips for p in c["places"]})}


def text(value, label, limit, required=True):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise ValueError(f"{label}不能为空且不能超过 {limit} 字。" if required else f"{label}格式不正确或过长。")
    return value.strip()


def validate(value, variant_id):
    from pipeline import check_script
    if not isinstance(value, dict) or not isinstance(value.get("lines"), list) or len(value["lines"]) != 5:
        raise ValueError("当前制作流程需要完整的 5 句脚本。")
    vocab = vocabulary()
    variant = {"id": variant_id, "idea": text(value.get("idea", ""), "开头思路", 500, False), "lines": []}
    for i, line in enumerate(value["lines"]):
        if not isinstance(line, dict):
            raise ValueError("脚本句子格式不正确。")
        clean = {k: text(line.get(k, ""), f"第 {i+1} 句{label}", limit, required)
                 for k, label, limit, required in [("en", "英文", 1000, True), ("zh", "中文", 1000, True),
                                                   ("role", "用途", 100, False), ("on_screen", "花字", 200, False)]}
        for key in ("tags", "places"):
            items = line.get(key, [])
            if not isinstance(items, list) or any(not isinstance(x, str) or x not in vocab[key] for x in items):
                raise ValueError(f"第 {i+1} 句的画面标签或地点不在素材库中。")
            if key == "tags" and not 1 <= len(items) <= 3:
                raise ValueError(f"第 {i+1} 句请选择 1–3 个画面标签。")
            clean[key] = list(dict.fromkeys(items))
        variant["lines"].append(clean)
    claims = value.get("claims", [])
    if not isinstance(claims, list) or len(claims) > 30:
        raise ValueError("待核对事实格式不正确。")
    variant["claims"] = [text(c, "待核对事实", 1000) for c in claims]
    variant["checks"] = check_script(variant, load("facts.json"))
    variant["ok"] = all(c["ok"] for c in variant["checks"])
    return variant


def rewrite(session, variant_id, instruction):
    original = next((v for v in session["scripts"]["variants"] if v["id"] == variant_id), None)
    if original is None:
        raise ValueError("请选择要改写的脚本。")
    instruction = text(instruction, "修改意见", 2000)
    system = (DATA / "script_prompt.md").read_text(encoding="utf-8")
    system += '\n本次只改写用户指定的一版，保留未要求改变的事实和内容。只输出 JSON：{"variant": {"idea": "中文思路", "lines": [{"en": "英文", "zh": "中文", "role": "用途", "on_screen": "花字或空字符串", "tags": ["画面标签"], "places": ["地点"]}], "claims": []}}。lines 必须有 5 句。中英文、花字、画面标签和地点须随口播同步更新。tags 每句 1–3 个，tags 和 places 只能选素材词表中的值。未验证的具体事实列入 claims。不要引入第三方品牌，不要把示例优惠当作已核实事实。'
    user = {"选题": session["title"], "国家": session["country"], "平台": session["platform"],
            "原稿": original, "修改意见": instruction, "事实库": load("facts.json"), "素材词表": vocabulary()}
    data, usage = llm.script_json(system, json.dumps(user, ensure_ascii=False))
    return {"variant": validate(data.get("variant"), variant_id), "usage": usage}


def derive(session, variant_id, mode, kept="", changes=""):
    """基于母稿给出 3 个复用/裂变方向；不改动母稿。"""
    modes = {"topic": "复用选题", "structure": "复用结构", "fission": "裂变新选题"}
    if mode not in modes:
        raise ValueError("请选择复用或裂变方式。")
    original = next((v for v in session["scripts"]["variants"] if v["id"] == variant_id), None)
    if original is None:
        raise ValueError("找不到母稿，请刷新脚本库。")
    kept = text(kept or "", "必留要素", 1000, False)
    changes = text(changes or "", "变化条件", 1000, False)
    rules = {"topic": "沿用母稿的路线或核心问题，换人群、切入角度、时长或利益点。",
             "structure": "保留母稿有依据的叙事顺序（问题开头→体验→利益点→行动引导），换路线、场景与事实。",
             "fission": "按人群、痛点、场景、路线、营销节点或表现形式展开 3 个差异明显的方向。"}[mode]
    system = (DATA / "script_prompt.md").read_text(encoding="utf-8")
    system += f'''

现在只做选题方向，不写完整脚本。方式：{modes[mode]}。规则：{rules}
只输出 JSON：{{"directions": [{{"title": "不超过 30 字的选题", "audience": "面向谁", "angle": "切入角度", "difference": "和母稿的区别", "keep": "必须保留的要素", "check": "需要重新核实的事实，没有就写无"}}]}}
必须给 3 个差异明确的方向，不能只换几个词。不要编造价格、班次、优惠和素材授权；没有依据的写"待核实"。'''
    user = {"母稿题目": session["title"], "国家": session["country"], "平台": session["platform"],
            "母稿": original, "必留要素": kept, "变化条件": changes,
            "事实库": load("facts.json"), "素材词表": vocabulary()}
    data, usage = llm.script_json(system, json.dumps(user, ensure_ascii=False))
    items = data.get("directions")
    if not isinstance(items, list) or len(items) != 3:
        raise ValueError("模型没有返回 3 个方向，请再试一次。")
    clean = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("方向格式不正确，请再试一次。")
        clean.append({key: text(item.get(key, ""), label, limit, key == "title")
                      for key, label, limit in [("title", "方向题目", 60), ("audience", "人群", 200),
                                                ("angle", "角度", 200), ("difference", "区别", 200),
                                                ("keep", "必留", 200), ("check", "待核实", 200)]})
    return {"mode": mode, "directions": clean, "usage": usage}
