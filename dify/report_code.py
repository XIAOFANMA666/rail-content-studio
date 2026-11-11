"""Dify 代码节点：证据受限的七维评测和人工核验。"""
import json
import math
import re

RUBRIC = [
    ('开头吸引力', [('opening_hook','前 3 秒有吸引点',8,4,False), ('opening_topic','主题明确',6,3,False), ('opening_interest','有继续看下去的理由',6,3,False)]),
    ('内容价值与故事', [('content_value','内容有价值',8,4,True), ('content_story','故事容易理解',6,3,True), ('content_fact','内容真实可信',6,3,False)]),
    ('画面与内容匹配', [('visual_match','画面贴合口播',6,3,True), ('visual_location','地点和场景准确',5,2,False), ('visual_appeal','画面具有吸引力',4,2,True)]),
    ('剪辑节奏与完整度', [('editing_pace','镜头节奏',6,3,False), ('editing_sync','画面与配音衔接',5,2,False), ('editing_complete','视频完整',4,2,False)]),
    ('配音与声音', [('sound_clear','配音清晰',4,2,False), ('sound_voice','语气与语速',3,1.5,False), ('sound_background','背景声音',3,1.5,False)]),
    ('字幕与手机观看体验', [('subtitle_correct','字幕准确',4,2,False), ('subtitle_readable','字幕易读',3,1.5,False), ('subtitle_mobile','手机画面适配',3,1.5,False)]),
    ('行动引导与分享意愿', [('cta_clear','行动引导清楚',4,2,False), ('cta_natural','引导自然',3,1.5,False), ('cta_share','有收藏或分享理由',3,1.5,True)]),
]
RULES = [('face','人脸'), ('flag','国旗'), ('competitor_logo','竞品 Logo')]


def _parse_model(value):
    text = value.strip() if isinstance(value, str) else ''
    while text.startswith('<think>'):
        end = text.find('</think>')
        if end < 0:
            return {}
        text = text[end + 8:].strip()
    if text.startswith('```'):
        match = re.fullmatch(r'```(?:json)?\s*\n?(.*?)\s*```', text, flags=re.S | re.I)
        if not match:
            return {}
        text = match.group(1).strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _numeric(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def main(body: str, status: int, script: str, model_text: str, image_status: int, manual_review: str) -> dict:
    try:
        if status != 200:
            raise ValueError('本机视频分析失败，请检查辅助服务。')
        data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError('视频分析结果格式无效。')
        frames = data.get('frames')
        if not isinstance(frames, list) or not frames:
            raise ValueError('没有可用的真实抽帧。')
        times = [f.get('time_sec') for f in frames if isinstance(f,dict) and _numeric(f.get('time_sec')) and f['time_sec'] >= 0]
        if not times:
            raise ValueError('抽帧时间无效。')
        try:
            manual = json.loads(manual_review) if manual_review and manual_review.strip() else {}
        except (ValueError, TypeError):
            raise ValueError('人工核验必须填写有效 JSON。')
        if not isinstance(manual, dict):
            raise ValueError('人工核验必须为 JSON 对象。')
        scores, risk_confirm = manual.get('scores', {}), manual.get('risk', {})
        if not isinstance(scores,dict) or not isinstance(risk_confirm,dict):
            raise ValueError('人工核验的 scores 和 risk 必须是对象。')
        known = {item[0]: item for _, items in RUBRIC for item in items}
        if set(scores)-set(known) or set(risk_confirm)-{r[0] for r in RULES}:
            raise ValueError('人工核验含未知项目，请核对项目编号。')
        for key,value in scores.items():
            item = known[key]
            if not _numeric(value) or value not in (0,item[3],item[2]):
                raise ValueError('人工分数无效：'+key+' 只能填写 0、部分分或满分。')
        for value in risk_confirm.values():
            if value not in ('found','absent','unknown'):
                raise ValueError('风险核验只能填写 found、absent 或 unknown。')
        model = _parse_model(model_text) if image_status == 200 else {}
        rows = model.get('criteria', []) if isinstance(model.get('criteria'), list) else []
        by_id = {r.get('id'):r for r in rows if isinstance(r,dict)}
        transcript = data.get('transcript') if isinstance(data.get('transcript'),dict) else {}
        transcript_text = ' '.join(s.get('text','') for s in transcript.get('segments',[]) if isinstance(s,dict) and isinstance(s.get('text'),str)) if isinstance(transcript.get('segments'),list) else ''
        source_text = (script or '') + '\n' + transcript_text
        def valid_evidence(row):
            evidence = row.get('evidence')
            if not isinstance(evidence, list) or not evidence:
                return False
            for e in evidence:
                if not isinstance(e,dict) or not isinstance(e.get('observation'),str) or not e['observation'].strip():
                    return False
                time = e.get('time_sec')
                quote = e.get('excerpt')
                has_time = _numeric(time) and any(abs(time-t)<0.15 for t in times)
                has_quote = isinstance(quote,str) and bool(quote.strip()) and quote in source_text
                if not (has_time or has_quote):
                    return False
            return True
        dims = []
        for name,items in RUBRIC:
            out = []
            for key,label,maximum,partial,ai in items:
                row = by_id.get(key,{}) if ai else {}
                val = row.get('score')
                valid = ai and _numeric(val) and val in (0,partial,maximum) and valid_evidence(row)
                if key in scores:
                    score,source,reason = scores[key],'人工','人工评分，请保留人工观看依据。'
                elif valid:
                    score,source,reason = val,'AI 初评','抽样画面或转写证据，仅供人工复核。'
                else:
                    score,source,reason = None,'待确认','抽样或转写不足以可靠评分，请观看完整视频并人工核验。'
                out.append({'id':key,'name':label,'max':maximum,'partial':partial,'score':score,'source':source,'reason':reason,'evidence':row.get('evidence',[]) if valid else [], 'suggestion':row.get('suggestion','') if valid and isinstance(row.get('suggestion',''),str) else ''})
            dims.append({'name':name,'max':sum(x[2] for x in items),'criteria':out})
        model_risks = model.get('risks',[]) if isinstance(model.get('risks'),list) else []
        risks_by_id = {r.get('id'):r for r in model_risks if isinstance(r,dict)}
        risks = []
        for key,label in RULES:
            row=risks_by_id.get(key,{})
            timestamp = row.get('time_sec')
            observation = row.get('observation')
            suspected = row.get('status')=='suspected' and _numeric(timestamp) and any(abs(timestamp-t)<0.15 for t in times) and isinstance(observation,str) and bool(observation.strip())
            risks.append({'id':key,'name':label,'manual':risk_confirm.get(key,'unknown'),'ai_status':'suspected' if suspected else 'unknown','evidence':('抽帧 '+str(timestamp)+' 秒：'+' '.join(observation.split())) if suspected else '抽帧不能排除全片风险，需人工核验。'})
        states=[r['manual'] for r in risks]
        verdict = '不合格' if 'found' in states else '待人工核验' if 'unknown' in states else '三项否决规则已人工确认未出现'
        criteria=[c for dim in dims for c in dim['criteria']]
        assessed=[c for c in criteria if c['score'] is not None]
        total=round(sum(c['score'] for c in assessed),1) if len(assessed)==len(criteria) else None
        assessed_points=round(sum(c['score'] for c in assessed),1)
        assessed_max=sum(c['max'] for c in assessed)
        lines=['## 评分','总分：'+(str(total)+' / 100' if total is not None else '待确认（已评 '+str(assessed_points)+' / '+str(assessed_max)+' 分）')]
        suggestions=[]
        for dim in dims:
            lines.append('\n'+dim['name']+'（'+str(dim['max'])+' 分）')
            for c in dim['criteria']:
                lines.append(c['id']+' '+c['name']+'：'+(str(c['score'])+' / '+str(c['max'])+'（'+c['source']+'）' if c['score'] is not None else '待确认（部分 '+str(c['partial'])+'，满分 '+str(c['max'])+'）')+'；'+c['reason'])
                if c['evidence']:
                    lines.append('证据：'+json.dumps(c['evidence'],ensure_ascii=False))
                if c['suggestion'].strip():
                    suggestions.append(c['name']+'：'+' '.join(c['suggestion'].split()))
        lines.append('\nAI 只分析抽样帧及转写，未观看全片；转写可能有错误。实际音质、节奏、字幕与事实需要人工核验。分数不能预测爆款。')
        safety=['## 安全、转人工','判定：'+verdict,'人脸、国旗或竞品 Logo 任一人工确认出现即不合格，与分数无关。']
        labels={'found':'确认出现','absent':'确认未出现','unknown':'待确认'}
        for r in risks:
            safety.append(r['name']+'：人工'+labels[r['manual']]+'；AI'+('疑似出现' if r['ai_status']=='suspected' else '待确认')+'；'+r['evidence'])
        safety.append('请人工观看完整视频并检查事实、素材使用权和平台规则。人工核验可在重新运行时以 JSON 提交；found=确认出现，absent=确认未出现，unknown=待确认。')
        report='\n'.join(lines)+'\n\n'+'\n'.join(safety)
        if suggestions:
            report+='\n\n## 优化建议\n'+'\n'.join('- '+s for s in suggestions)
        return {'report':report}
    except (ValueError,TypeError,KeyError,IndexError) as exc:
        return {'report':'## 评分\n评测失败：'+str(exc)+'\n\n## 安全、转人工\n待人工核验：评测未完成，不能确认安全。'}
