"""能从粗剪数据直接算出来的评分项。规则写死，不交给 AI 猜，也不用人填。

输入是流水线保存的 output/result.json（含剪辑规则 skill），以及导出视频的时长和语音转写。
每一项返回 (分数, 说明)；数据缺失时分数为 None，由调用方交给 AI 或人。
"""
import difflib
import re

DURATION_TOLERANCE_S = 0.5
ITEMS = ('editing_pace', 'editing_sync', 'editing_complete', 'visual_location', 'content_fact',
         'subtitle_correct', 'subtitle_readable', 'cta_clear')


def matches(result, duration):
    """导出视频和这次粗剪是不是同一条：时长相差不超过 0.5 秒。返回 (是否同一条, 相差秒数)。"""
    try:
        diff = abs(float(result['total_s']) - float(duration))
    except (KeyError, TypeError, ValueError):
        return False, None
    return diff <= DURATION_TOLERANCE_S, round(diff, 2)


def _segments(result):
    return result.get('segments') or []


def _shots(result):
    return [shot for seg in _segments(result) for shot in seg.get('shots') or []]


def editing_pace(result, skill):
    shots, limit = _shots(result), skill.get('max_shot_s')
    if not shots or not isinstance(limit, (int, float)):
        return None, '草稿里没有镜头，或缺少镜头时长规则。'
    off = [s for s in shots if not 1.0 <= s['dur'] <= limit + 0.05]
    score = 6 if not off else 3 if len(off) <= 2 else 0
    return score, f'{len(shots)} 个镜头，{len(off)} 个不在 1.0–{limit:g} 秒范围内。'


def editing_sync(result):
    later = _segments(result)[1:]
    cuts = [s['start'] for s in _shots(result)]
    if not cuts:
        return None, '草稿里没有镜头。'
    if not later:
        return 5, '只有一句口播，不需要对齐切点。'
    hit = sum(any(abs(seg['start'] - cut) <= 0.15 for cut in cuts) for seg in later)
    score = 5 if hit == len(later) else 2 if hit * 2 >= len(later) else 0
    return score, f'{len(later)} 处换句，{hit} 处在 0.15 秒内有镜头切点。'


def editing_complete(has_video, has_audio):
    if has_video and has_audio:
        return 4, '导出视频时长与草稿一致，画面和声音都在。'
    return 0, '导出视频缺少画面或声音。'


def visual_location(result):
    shots = _shots(result)
    if not shots:
        return None, '草稿里没有镜头。'
    wrong = [s for s in shots if s.get('excluded')]
    if wrong:
        return 0, f'{len(wrong)} 个镜头的素材地点和口播不符：' + '、'.join(str(s.get('clip')) for s in wrong) + '。'
    return 5, f'{len(shots)} 个镜头的素材地点标签都和口播地点相符。'


def content_fact(result):
    """返回 (分数, 说明, 待人工核对的说法)。"""
    scripts = result.get('scripts') or {}
    chosen = next((v for v in scripts.get('variants') or [] if v.get('id') == scripts.get('chosen')), None)
    if chosen is None:
        return None, '找不到采用的脚本版本。', []
    claims = [str(c) for c in chosen.get('claims') or [] if str(c).strip()]
    if not chosen.get('ok'):
        return 0, '采用的脚本没有通过规则和事实库审核。', claims
    if claims:
        return 3, f'脚本通过审核，但有 {len(claims)} 处说法在事实库里查不到，要人核对。', claims
    return 6, '脚本通过规则和事实库审核，没有待核对的说法。', []


def _norm(text):
    return re.sub(r'[^a-z0-9]', '', str(text).lower())


def similarity(a, b):
    """只比字母和数字，忽略大小写、空格和标点。"""
    return difflib.SequenceMatcher(None, _norm(a), _norm(b), autojunk=False).ratio()


def subtitle_correct(result, transcript):
    subtitles = ' '.join(seg.get('subtitle') or '' for seg in _segments(result))
    if not _norm(subtitles) or not _norm(transcript):
        return None, '缺少字幕或语音转写，无法比对。'
    ratio = similarity(subtitles, transcript)
    score = 4 if ratio >= 0.92 else 2 if ratio >= 0.80 else 0
    return score, f'字幕和语音转写的相似度 {ratio:.0%}。'


def subtitle_readable(result, skill):
    limit = (skill.get('subtitle') or {}).get('max_chars_per_line')
    subtitles = [seg.get('subtitle') or '' for seg in _segments(result)]
    if not subtitles or not isinstance(limit, int):
        return None, '草稿里没有字幕，或缺少字幕行宽规则。'
    off = [s for s in subtitles if len(s.split('\n')) > 2 or any(len(line) > limit for line in s.split('\n'))]
    return (3 if not off else 1.5), f'{len(subtitles)} 条字幕，{len(off)} 条超过 2 行或单行超过 {limit} 个字符。'


def cta_clear(result, skill):
    need = skill.get('cta_min_s')
    if not isinstance(need, (int, float)):
        return None, '缺少行动引导停留时长规则。'
    hooks = [st for seg in _segments(result) for st in seg.get('stickers') or [] if st.get('kind') == '结尾钩子']
    if not hooks:
        return 0, '草稿里没有结尾的行动引导花字。'
    hook = max(hooks, key=lambda st: st.get('dur') or 0)
    shown, text = hook.get('dur') or 0, hook.get('text') or ''
    if shown >= need:
        return 4, f'结尾有行动引导花字「{text}」，停留 {shown:.1f} 秒。'
    return 2, f'结尾有行动引导花字「{text}」，只停留 {shown:.1f} 秒，少于 {need:g} 秒。'


def score_all(result, transcript, has_video=True, has_audio=True):
    """返回 ({项目 id: {'score', 'reason'}}, 待核对的说法)。某一项的数据有问题只影响它自己。"""
    skill = result.get('skill') or {}
    claims = []

    def fact():
        nonlocal claims
        score, reason, claims = content_fact(result)
        return score, reason

    rules = {
        'editing_pace': lambda: editing_pace(result, skill),
        'editing_sync': lambda: editing_sync(result),
        'editing_complete': lambda: editing_complete(has_video, has_audio),
        'visual_location': lambda: visual_location(result),
        'content_fact': fact,
        'subtitle_correct': lambda: subtitle_correct(result, transcript),
        'subtitle_readable': lambda: subtitle_readable(result, skill),
        'cta_clear': lambda: cta_clear(result, skill),
    }
    scored = {}
    for item in ITEMS:
        try:
            score, reason = rules[item]()
        except (KeyError, TypeError, ValueError, AttributeError):
            score, reason = None, '草稿数据格式不完整，这一项算不出来。'
        scored[item] = {'score': score, 'reason': reason}
    return scored, claims
