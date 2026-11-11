import importlib.util
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('cloud_report_code', ROOT / 'cloud_report_code.py')
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)
local_spec = importlib.util.spec_from_file_location('report_code', ROOT / 'report_code.py')
local = importlib.util.module_from_spec(local_spec)
local_spec.loader.exec_module(local)


def evaluate(visual=None, model=None, manual=None, transcript='', audio_status='unavailable'):
    return cloud.main(json.dumps(visual or {}), transcript, json.dumps(model or {}),
                      json.dumps(manual or {}), audio_status)['report']


def observation(time=2.1):
    return {'observations': [{'id': 'v1', 'time_sec': time, 'observation': '车站画面有明确旅行主题'}]}


def criterion(key='visual_appeal', score=2, evidence=None, suggestion=''):
    return {'criteria': [{'id': key, 'score': score,
                          'evidence': evidence if evidence is not None else [{'observation_id': 'v1'}],
                          'suggestion': suggestion}]}


class CloudReportTest(unittest.TestCase):
    def test_cloud_preserves_exact_rubric_and_unknown_not_zero(self):
        self.assertEqual(cloud.RUBRIC, local.RUBRIC)
        self.assertEqual(sum(item[2] for _, items in cloud.RUBRIC for item in items), 100)
        out = evaluate()
        self.assertIn('总分：待确认（已评 0 / 0 分）', out)
        self.assertIn('判定：待人工核验', out)
        self.assertEqual([line for line in out.splitlines() if line.startswith('## ')],
                         ['## 评分', '## 安全、转人工'])

    def test_native_video_observation_is_honestly_labelled(self):
        out = evaluate(observation(), criterion(suggestion='增加近景变化'))
        self.assertIn('visual_appeal 画面具有吸引力：2 / 4（AI 初评）', out)
        self.assertIn('AI 估计位置 2.1 秒（待复核）', out)
        self.assertIn('未经独立抽帧核对', out)
        self.assertIn('## 优化建议\n- 画面具有吸引力：增加近景变化', out)

    def test_unbacked_or_duplicate_visual_evidence_cannot_score(self):
        wrong_ref = criterion(evidence=[{'observation_id': 'v9'}], suggestion='无效建议')
        self.assertNotIn('## 优化建议', evaluate(observation(), wrong_ref))
        duplicate = observation()
        duplicate['observations'].append({'id': 'v1', 'time_sec': 1, 'observation': '冲突观察'})
        self.assertIn('visual_appeal 画面具有吸引力：待确认', evaluate(duplicate, criterion()))
        for invalid in (True, -1, float('nan'), float('inf'), '2'):
            self.assertIn('visual_appeal 画面具有吸引力：待确认', evaluate(observation(invalid), criterion()))

    def test_raw_model_time_is_not_a_verified_frame(self):
        fabricated = criterion(evidence=[{'time_sec': 2.1, 'observation': '伪造帧'}], suggestion='无效建议')
        out = evaluate(observation(), fabricated)
        self.assertIn('visual_appeal 画面具有吸引力：待确认', out)
        self.assertNotIn('## 优化建议', out)

    def test_visual_match_needs_both_actual_audio_and_visual(self):
        evidence = [{'observation_id': 'v1'}, {'excerpt': 'Take the train to London.'}]
        model = criterion('visual_match', 3, evidence)
        for transcript, status in [('', 'unavailable'), ('Take the train to London.', 'unavailable'),
                                   ('not a valid audio file', 'ok')]:
            self.assertIn('visual_match 画面贴合口播：待确认', evaluate(observation(), model, transcript=transcript, audio_status=status))
        out = evaluate(observation(), model, transcript='Take the train to London.', audio_status='ok')
        self.assertIn('visual_match 画面贴合口播：3 / 6（AI 初评）', out)
        self.assertIn('实际音轨转写摘录', out)

    def test_audio_error_or_invented_quote_is_not_speech(self):
        for transcript in ('not a valid audio file', 'Error: timeout', 'Failed to transcribe video'):
            model = criterion('content_value', 4, [{'excerpt': transcript}])
            self.assertIn('content_value 内容有价值：待确认', evaluate(model=model, transcript=transcript, audio_status='ok'))
        model = criterion('content_value', 4, [{'excerpt': 'not spoken'}])
        self.assertIn('content_value 内容有价值：待确认', evaluate(model=model, transcript='actual speech', audio_status='ok'))

    def test_transcript_does_not_score_image_or_sound_quality(self):
        for key, score in [('visual_appeal', 2), ('sound_clear', 4), ('opening_hook', 8), ('subtitle_correct', 4)]:
            out = evaluate(model=criterion(key, score, [{'excerpt': 'London'}]), transcript='London', audio_status='ok')
            line = next(line for line in out.splitlines() if line.startswith(key + ' '))
            self.assertIn('待确认', line)

    def test_risks_are_never_auto_safe_and_manual_found_is_hard_fail(self):
        for risk in ('face', 'flag', 'competitor_logo'):
            visual = observation()
            visual['risks'] = [{'id': risk, 'status': 'suspected', 'observation_ids': ['v1']}]
            out = evaluate(visual)
            self.assertIn('AI疑似出现', out)
            self.assertIn('判定：待人工核验', out)
            self.assertIn('判定：不合格', evaluate(visual, manual={'risk': {risk: 'found'}}))
            visual['risks'][0]['status'] = 'absent'
            self.assertNotIn('AI疑似出现', evaluate(visual))
        out = evaluate({'risks': [{'id': 'face', 'status': 'suspected', 'observation_ids': ['v1']}]})
        self.assertNotIn('AI疑似出现', out)

    def test_complete_manual_can_finish_even_when_ai_unavailable(self):
        scores = {item[0]: item[2] for _, items in cloud.RUBRIC for item in items}
        checks = {key: 'absent' for key, _ in cloud.RULES}
        out = evaluate(manual={'scores': scores, 'risk': checks})
        self.assertIn('总分：100 / 100', out)
        self.assertIn('三项否决规则已人工确认未出现', out)
        checks['flag'] = 'found'
        self.assertIn('判定：不合格', evaluate(manual={'scores': scores, 'risk': checks}))

    def test_malformed_manual_or_model_remains_safe(self):
        for manual in ('{', '[]', '{"scores":{"sound_clear":true}}', '{"risk":{"face":"pass"}}'):
            out = cloud.main('{}', '', '{}', manual)['report']
            self.assertIn('评测失败', out)
            self.assertIn('待人工核验', out)
        out = cloud.main('<think>unfinished', '', '[]', '')['report']
        self.assertIn('总分：待确认', out)
        self.assertNotIn('## 优化建议', out)


if __name__ == '__main__':
    unittest.main()
