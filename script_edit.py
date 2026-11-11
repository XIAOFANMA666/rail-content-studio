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
