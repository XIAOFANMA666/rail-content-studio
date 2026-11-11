"""成片评测的离线测试：不调用任何接口，模型回答都是假的。"""
import re
import unittest

import video_review as vr

USAGE = {'prompt_tokens': 100, 'completion_tokens': 10}


def frames(count):
    return [{'frame': i, 'time': vr.frame_time(i), 'b64': ''} for i in range(1, count + 1)]


def sent(content):
    """从请求内容里读出这次发了哪几帧。"""
    return [int(m.group(1)) for part in content if part['type'] == 'text'
            for m in [re.match(r'第 (\d+) 帧', part['text'])] if m]


def answer(numbers, flag=(), face=(), logo=()):
    return {'frames': [{'frame': n, 'see': '山和铁轨', 'face': '有人露出脸' if n in face else '',
                        'flag': '德国国旗' if n in flag else '', 'logo': 'vivo' if n in logo else ''} for n in numbers]}


class FrameTests(unittest.TestCase):
    def test_batches_are_small_and_continuous(self):
        groups = vr.batches(frames(150))
        self.assertEqual(len(groups), 25)
        self.assertTrue(all(len(g) == vr.BATCH for g in groups))
        self.assertEqual([f['frame'] for g in groups for f in g], list(range(1, 151)))

    def test_pick_frames_keeps_the_first_three_seconds(self):
        picked = vr.pick_frames(frames(200))
        self.assertEqual(len(picked), vr.SCORE_FRAMES)
        self.assertEqual([f['frame'] for f in picked[:6]], [1, 2, 3, 4, 5, 6])
        self.assertEqual(len({f['frame'] for f in picked}), vr.SCORE_FRAMES)

    def test_merge_spans_converts_frames_to_seconds(self):
        hits = [{'frame': n, 'type': 'flag', 'what': '国旗'} for n in (29, 30, 31, 32, 43, 44)]
        spans = vr.merge_spans(hits, 'flag')
        self.assertEqual([(s['start'], s['end'], s['frames']) for s in spans],
                         [(14.0, 16.0, [29, 30, 31, 32]), (21.0, 22.0, [43, 44])])
        self.assertEqual(vr.merge_spans(hits, 'face'), [])


class ParseTests(unittest.TestCase):
    def test_only_complete_rows_count_as_answered(self):
        raw = {'frames': [
            {'frame': 1, 'see': '雪山', 'face': '', 'flag': '', 'logo': ''},
            {'frame': 2, 'see': '', 'face': '', 'flag': '瑞士国旗', 'logo': ''},      # 没写看到什么
            {'frame': 3, 'see': '手机', 'face': '无', 'flag': 'None', 'logo': 'vivo'},  # "无"不算命中
            {'frame': 9, 'see': '不在这一批', 'face': '', 'flag': '国旗', 'logo': ''},
            {'frame': 4, 'see': '车站', 'face': None, 'flag': '', 'logo': ''},          # 字段类型不对
            {'frame': 1, 'see': '重复', 'face': '', 'flag': '国旗', 'logo': ''},
        ]}
        hits, answered = vr.parse_rows(raw, {1, 2, 3, 4})
        self.assertEqual(answered, {1, 3})
        self.assertEqual(hits, [{'frame': 3, 'type': 'logo', 'what': 'vivo'}])

    def test_wrong_shape_is_an_error(self):
        with self.assertRaises(ValueError):
            vr.parse_rows({'hits': []}, {1})


class ComplianceTests(unittest.TestCase):
    def test_clear_and_hit(self):
        def call(key, model, content, max_tokens):
            return answer(sent(content), flag={8, 9}), USAGE
        risks, coverage, usages = vr.check_compliance(frames(12), 'k', 'm', call)
        status = {r['id']: r['status'] for r in risks}
        self.assertEqual(status, {'face': 'clear', 'flag': 'hit', 'logo': 'clear'})
        self.assertEqual(coverage['checked'], 12)
        self.assertTrue(coverage['complete'])
        self.assertEqual(len(usages), 2)

    def test_skipped_frame_is_asked_again_alone(self):
        asked = []

        def call(key, model, content, max_tokens):
            numbers = sent(content)
            asked.append(numbers)
            return answer([n for n in numbers if n != 3 or len(numbers) == 1]), USAGE
        risks, coverage, _ = vr.check_compliance(frames(6), 'k', 'm', call)
        self.assertEqual(asked, [[1, 2, 3, 4, 5, 6], [3]])
        self.assertTrue(coverage['complete'])

    def test_frame_still_missing_after_retry_is_incomplete(self):
        def call(key, model, content, max_tokens):
            return answer([n for n in sent(content) if n != 3]), USAGE
        risks, coverage, _ = vr.check_compliance(frames(6), 'k', 'm', call)
        self.assertEqual({r['status'] for r in risks}, {'incomplete'})
        self.assertFalse(coverage['complete'])
        self.assertEqual(coverage['checked'], 5)
        self.assertEqual(coverage['unchecked'], [[1.0, 1.5]])
        verdict, _ = vr.conclude(dims(), risks, coverage, {'matched': True, 'claims': []})
        self.assertEqual(verdict['result'], '转人工')

    def test_one_failed_batch_does_not_hide_the_rest(self):
        def call(key, model, content, max_tokens):
            numbers = sent(content)
            if 1 in numbers:
                raise RuntimeError('DeepSeek 请求失败（HTTP 500）')
            return answer(numbers, face={8}), USAGE
        risks, coverage, _ = vr.check_compliance(frames(12), 'k', 'm', call)
        self.assertEqual({r['id']: r['status'] for r in risks}, {'face': 'hit', 'flag': 'incomplete', 'logo': 'incomplete'})
        self.assertEqual(coverage['checked'], 6)

    def test_total_failure_is_reported_to_the_user(self):
        def call(key, model, content, max_tokens):
            raise RuntimeError('DeepSeek 请求失败（HTTP 401），请检查密钥、额度和模型权限。')
        with self.assertRaisesRegex(RuntimeError, '401'):
            vr.check_compliance(frames(12), 'k', 'm', call)


class ScoreTests(unittest.TestCase):
    def test_sound_scores_come_from_measurements(self):
        segments = [{'start': 0, 'end': 10, 'text': 'word ' * 25, 'avg_logprob': -0.2}]  # 150 词/分钟
        good = vr.score_sound({'lufs': -16.0, 'speech_db': -14.0, 'quiet_db': -80.0}, segments)
        self.assertEqual([good[k]['score'] for k in ('sound_clear', 'sound_voice', 'sound_background')], [4, 3, 3])
        self.assertTrue(all(row['source'] == 'measure' for row in good.values()))
        quiet_and_fast = vr.score_sound({'lufs': -30.0, 'speech_db': -28.0, 'quiet_db': -36.0},
                                        [{'start': 0, 'end': 10, 'text': 'word ' * 34, 'avg_logprob': -0.2}])  # 204 词/分钟
        self.assertEqual([quiet_and_fast[k]['score'] for k in ('sound_clear', 'sound_voice', 'sound_background')], [2, 1.5, 1.5])
        bad = vr.score_sound({'lufs': -30.0, 'speech_db': -20.0, 'quiet_db': -22.0},
                             [{'start': 0, 'end': 10, 'text': 'word ' * 50, 'avg_logprob': -0.9}])
        self.assertEqual([bad[k]['score'] for k in ('sound_clear', 'sound_voice', 'sound_background')], [0, 0, 0])
        self.assertIsNone(vr.score_sound({'lufs': None, 'speech_db': None, 'quiet_db': None}, segments)['sound_clear']['score'])

    def test_mobile_fit(self):
        self.assertEqual(vr.score_mobile(1080, 1920)['score'], 3)
        self.assertEqual(vr.score_mobile(1920, 1080)['score'], 0)
        self.assertEqual(vr.score_mobile(540, 960)['score'], 0)
        self.assertIsNone(vr.score_mobile(0, 0)['score'])

    def test_ai_score_must_use_listed_points(self):
        replies = iter([
            {'summary': '还行', 'criteria': [
                {'id': 'opening_hook', 'score': 8, 'confidence': 'high', 'evidence': '第 2 帧', 'suggestion': ''},
                {'id': 'opening_topic', 'score': 5, 'confidence': 'high', 'evidence': '第 3 帧', 'suggestion': ''}]},   # 5 不是可选分数
            {'summary': '还行', 'criteria': [
                {'id': 'opening_topic', 'score': 3, 'confidence': 'low', 'evidence': '第 3 帧', 'suggestion': '点题'},
                {'id': 'opening_hook', 'score': 0, 'confidence': 'high', 'evidence': '改口了', 'suggestion': ''}]}])
        scored, summary, usages = vr.ai_score(frames(8), [{'start': 0, 'end': 2, 'text': 'hi'}], 'hi',
                                              ['opening_hook', 'opening_topic', 'opening_interest'], 'k', 'm',
                                              lambda *a: (next(replies), USAGE))
        self.assertEqual(scored['opening_hook']['score'], 8)       # 第一次合格的结果不被重试覆盖
        self.assertEqual((scored['opening_topic']['score'], scored['opening_topic']['confidence']), (3, 'low'))
        self.assertIsNone(scored['opening_interest']['score'])     # 两次都没给，留空
        self.assertEqual((summary, len(usages)), ('还行', 2))


def dims(overrides=None):
    """所有项目满分、高置信的评分表，再按需要改几项。"""
    scored = {cid: {'score': maximum, 'source': 'ai', 'confidence': 'high', 'reason': '依据'}
              for cid, (maximum, _) in vr.POINTS.items()}
    for cid, change in (overrides or {}).items():
        scored[cid] = dict(scored[cid], **change)
    return vr.assemble(scored)


def clear_risks(hit=None):
    risks = [{'id': kind, 'name': name, 'status': 'clear', 'spans': []} for kind, name in vr.RISKS]
    if hit:
        risk = next(r for r in risks if r['id'] == hit)
        risk.update(status='hit', spans=[{'start': 14.0, 'end': 16.0, 'what': '德国国旗', 'frames': [29, 30, 31, 32]}])
    return risks


FULL = {'fps': 2, 'frames': 47, 'checked': 47, 'complete': True, 'unchecked': []}
MATCHED = {'matched': True, 'claims': []}


class ConcludeTests(unittest.TestCase):
    def test_rubric_adds_up_to_100(self):
        self.assertEqual(sum(d['max'] for d in dims()), 100)
        self.assertEqual(len(vr.POINTS), 21)
        self.assertEqual(set(vr.AI_ITEMS) | set(vr.FALLBACK_ITEMS) | {'sound_clear', 'sound_voice', 'sound_background', 'subtitle_mobile'},
                         set(vr.POINTS))

    def test_pass(self):
        verdict, total = vr.conclude(dims(), clear_risks(), FULL, MATCHED)
        self.assertEqual((verdict, total), ({'result': '通过', 'reasons': []}, 100))

    def test_flag_goes_to_human_with_the_time_span(self):
        verdict, _ = vr.conclude(dims(), clear_risks('flag'), FULL, MATCHED)
        self.assertEqual(verdict, {'result': '转人工', 'reasons': ['国旗：14.0–16.0 秒，德国国旗']})

    def test_each_other_reason_sends_to_human(self):
        cases = {
            '没检查成功': (dims(), clear_risks(), dict(FULL, checked=41, complete=False, unchecked=[[1.0, 4.0]]), MATCHED),
            '无法自动评分：背景声音': (dims({'sound_background': {'score': None}}), clear_risks(), FULL, MATCHED),
            '3 项 AI 把握不足': (dims({k: {'confidence': 'low'} for k in ('opening_hook', 'visual_match', 'cta_share')}), clear_risks(), FULL, MATCHED),
            '低于 70 分': (dims({k: {'score': 0} for k in ('opening_hook', 'opening_topic', 'opening_interest', 'content_value', 'content_story')}), clear_risks(), FULL, MATCHED),
            '待核对的说法：伦敦到爱丁堡 4 小时': (dims({'content_fact': {'score': 3}}), clear_risks(), FULL, {'matched': True, 'claims': ['伦敦到爱丁堡 4 小时']}),
        }
        for expected, args in cases.items():
            verdict, _ = vr.conclude(*args)
            self.assertEqual(verdict['result'], '转人工', expected)
            self.assertEqual(len(verdict['reasons']), 1, expected)
            self.assertIn(expected, verdict['reasons'][0])

    def test_two_low_confidence_items_still_pass(self):
        verdict, _ = vr.conclude(dims({k: {'confidence': 'low'} for k in ('opening_hook', 'visual_match')}), clear_risks(), FULL, MATCHED)
        self.assertEqual(verdict['result'], '通过')

    def test_unmatched_draft_is_named_as_the_cause(self):
        low = {k: {'confidence': 'low'} for k in vr.FALLBACK_ITEMS}
        verdict, _ = vr.conclude(dims(low), clear_risks(), FULL, {'matched': False, 'claims': []})
        self.assertIn('导出视频和当前草稿对不上', verdict['reasons'][0])


if __name__ == '__main__':
    unittest.main()
