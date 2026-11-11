"""Dify 云端代码节点：原生视频观察、转写与七维评分的证据校验。

visual_text: {"observations":[{"id":"v1","time_sec":1.2,"observation":"观察"}],
              "risks":[{"id":"face","status":"suspected","observation_ids":["v1"]}]}
model_text: {"criteria":[{"id":"visual_appeal","score":2,
             "evidence":[{"observation_id":"v1"}],"suggestion":"建议"}]}
声音证据使用 {"excerpt":"转写原文"}；audio_status 必须由流程确认成功后传入 ok。
模型给出的位置只是 AI 估计，绝不冒充独立抽帧程序实际返回的时间。
本文件可整体复制为 Dify Python3 代码节点，只依赖标准库。
"""
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
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _numeric(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _unique_rows(rows):
    """重复编号的模型输出不用于自动评分，避免后写值悄悄覆盖证据。"""
    indexed, duplicated = {}, set()
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            continue
        key = row['id']
        if key in indexed:
            duplicated.add(key)
        indexed[key] = row
    return {key: row for key, row in indexed.items() if key not in duplicated}


def _clean_text(text):
    return ' '.join(text.split()) if isinstance(text, str) else ''


def _valid_transcript(transcript, audio_status):
    if audio_status != 'ok' or not isinstance(transcript, str) or not transcript.strip():
        return ''
    normalized = transcript.strip().lower()
    # audio/asr 工具目前会把这些失败作为普通 text 返回；不能算作录音内容。
    if normalized in ('not a valid audio file', 'invalid audio file', 'no audio file provided'):
        return ''
    if normalized.startswith(('error:', 'error code:', 'traceback (most recent call last)', 'failed to transcribe')):
        return ''
    return transcript.strip()


def main(visual_text: str, transcript: str, model_text: str, manual_review: str,
         audio_status: str = 'unavailable') -> dict:
    try:
        try:
            manual = json.loads(manual_review) if isinstance(manual_review, str) and manual_review.strip() else {}
        except (ValueError, TypeError):
            raise ValueError('人工核验必须填写有效 JSON。')
        if not isinstance(manual, dict):
            raise ValueError('人工核验必须为 JSON 对象。')
        scores, risk_confirm = manual.get('scores', {}), manual.get('risk', {})
        if not isinstance(scores, dict) or not isinstance(risk_confirm, dict):
            raise ValueError('人工核验的 scores 和 risk 必须是对象。')
        known = {item[0]: item for _, items in RUBRIC for item in items}
        if set(scores) - set(known) or set(risk_confirm) - {rule[0] for rule in RULES}:
            raise ValueError('人工核验含未知项目，请核对项目编号。')
        for key, value in scores.items():
            item = known[key]
            if not _numeric(value) or value not in (0, item[3], item[2]):
                raise ValueError('人工分数无效：' + key + ' 只能填写 0、部分分或满分。')
        for value in risk_confirm.values():
            if value not in ('found', 'absent', 'unknown'):
                raise ValueError('风险核验只能填写 found、absent 或 unknown。')

        visual = _parse_model(visual_text)
        observations = {}
        for key, row in _unique_rows(visual.get('observations')).items():
            time = row.get('time_sec')
            observation = _clean_text(row.get('observation'))
            if re.fullmatch(r'v[1-9][0-9]{0,3}', key) and _numeric(time) and time >= 0 and observation:
                observations[key] = {'id': key, 'time_sec': time, 'observation': observation}
        actual_transcript = _valid_transcript(transcript, audio_status)
        model_rows = _unique_rows(_parse_model(model_text).get('criteria'))

        def validated_evidence(key, row):
            evidence = row.get('evidence')
            if not isinstance(evidence, list) or not evidence:
                return []
            out, visual_seen, audio_seen = [], False, False
            for item in evidence:
                if not isinstance(item, dict):
                    return []
                observation_id = item.get('observation_id')
                excerpt = item.get('excerpt')
                # 每条证据只能引用一种来源，避免无效引用被另一字段掩盖。
                if isinstance(observation_id, str) and excerpt is None and observation_id in observations:
                    observed = observations[observation_id]
                    out.append('视频 AI 观察 ' + observation_id + '；AI 估计位置 ' + str(observed['time_sec'])
                               + ' 秒（待复核）：' + observed['observation'])
                    visual_seen = True
                elif observation_id is None and isinstance(excerpt, str) and excerpt.strip() and actual_transcript and excerpt in actual_transcript:
                    out.append('实际音轨转写摘录（转写可能有误）：' + _clean_text(excerpt))
                    audio_seen = True
                else:
                    return []
            if key == 'visual_match' and not (visual_seen and audio_seen):
                return []
            if key == 'visual_appeal' and not visual_seen:
                return []
            return out

        dims = []
        for name, items in RUBRIC:
            criteria = []
            for key, label, maximum, partial, ai in items:
                row = model_rows.get(key, {}) if ai else {}
                evidence = validated_evidence(key, row) if ai else []
                val = row.get('score')
                valid = bool(evidence) and _numeric(val) and val in (0, partial, maximum)
                if key in scores:
                    score, source, reason = scores[key], '人工', '人工评分，请保留人工观看依据。'
                elif valid:
                    score, source, reason = val, 'AI 初评', '依据原视频的 AI 观察或实际音轨转写，仅供人工复核。'
                else:
                    score, source, reason = None, '待确认', '现有证据不足，请观看完整视频并人工核验。'
                criteria.append({'id': key, 'name': label, 'max': maximum, 'partial': partial,
                                 'score': score, 'source': source, 'reason': reason,
                                 'evidence': evidence if valid else [],
                                 'suggestion': _clean_text(row.get('suggestion')) if valid else ''})
            dims.append({'name': name, 'max': sum(item[2] for item in items), 'criteria': criteria})

        visual_risks = _unique_rows(visual.get('risks'))
        risks = []
        for key, label in RULES:
            row = visual_risks.get(key, {})
            ids = row.get('observation_ids')
            suspected = (row.get('status') == 'suspected' and isinstance(ids, list) and bool(ids)
                         and all(isinstance(ref, str) and ref in observations for ref in ids))
            evidence = []
            if suspected:
                for ref in dict.fromkeys(ids):
                    observed = observations[ref]
                    evidence.append('AI 估计位置 ' + str(observed['time_sec']) + ' 秒（待复核）：' + observed['observation'])
            risks.append({'id': key, 'name': label, 'manual': risk_confirm.get(key, 'unknown'),
                          'suspected': bool(suspected), 'evidence': evidence})

        states = [risk['manual'] for risk in risks]
        verdict = ('不合格' if 'found' in states else '待人工核验' if 'unknown' in states
                   else '三项否决规则已人工确认未出现')
        all_criteria = [c for dim in dims for c in dim['criteria']]
        assessed = [c for c in all_criteria if c['score'] is not None]
        points = round(sum(c['score'] for c in assessed), 1)
        maximum = sum(c['max'] for c in assessed)
        total = str(points) + ' / 100' if len(assessed) == len(all_criteria) else '待确认（已评 ' + str(points) + ' / ' + str(maximum) + ' 分）'
        lines = ['## 评分', '总分：' + total]
        suggestions = []
        for dim in dims:
            lines.append('\n' + dim['name'] + '（' + str(dim['max']) + ' 分）')
            for criterion in dim['criteria']:
                value = (str(criterion['score']) + ' / ' + str(criterion['max']) + '（' + criterion['source'] + '）'
                         if criterion['score'] is not None else '待确认（部分 ' + str(criterion['partial']) + '，满分 ' + str(criterion['max']) + '）')
                lines.append(criterion['id'] + ' ' + criterion['name'] + '：' + value + '；' + criterion['reason'])
                lines.extend('证据：' + evidence for evidence in criterion['evidence'])
                if criterion['suggestion']:
                    suggestions.append(criterion['name'] + '：' + criterion['suggestion'])
        lines.append('\n视频模型可能抽样分析并遗漏画面；所列时间为 AI 估计，未经独立抽帧核对，不能据此确认全片安全。实际音质、节奏、字幕与事实仍需人工核验，分数不能预测爆款。')
        if not actual_transcript:
            lines.append('本次没有可用的实际音轨转写，画面与口播匹配等待人工核验；未用预写脚本冒充录音内容。')
        safety = ['## 安全、转人工', '判定：' + verdict, '人脸、国旗或竞品 Logo 任一人工确认出现即不合格，与分数无关。']
        labels = {'found': '确认出现', 'absent': '确认未出现', 'unknown': '待确认'}
        for risk in risks:
            evidence = '；'.join(risk['evidence']) if risk['evidence'] else '视频 AI 未提供可定位的疑似证据，不能据此认定全片不存在。'
            safety.append(risk['name'] + '：人工' + labels[risk['manual']] + '；AI'
                          + ('疑似出现' if risk['suspected'] else '待确认') + '；' + evidence)
        safety.append('暂无竞品名单时，疑似品牌标识仍需人工确认是否属于竞品。请人工观看完整视频并检查事实、素材使用权和平台规则。人工核验 JSON 中 found=确认出现，absent=确认未出现，unknown=待确认。')
        report = '\n'.join(lines) + '\n\n' + '\n'.join(safety)
        if suggestions:
            report += '\n\n## 优化建议\n' + '\n'.join('- ' + suggestion for suggestion in suggestions)
        return {'report': report}
    except (ValueError, TypeError, KeyError, IndexError) as exc:
        return {'report': '## 评分\n评测失败：' + str(exc) + '\n\n## 安全、转人工\n待人工核验：评测未完成，不能确认安全。'}
