"""换模型的配置和蒸馏工具的离线测试。"""
import json
import os
import unittest
from unittest.mock import patch

import llm
from tools import distill_skill as ds

KEYS = ('DEEPSEEK_API_KEY', 'DEEPSEEK_VIDEO_MODEL', 'LLM_BASE_URL', 'LLM_API_KEY', 'SCRIPT_BASE_URL', 'SCRIPT_API_KEY',
        'SCRIPT_MODEL', 'VIDEO_BASE_URL', 'VIDEO_API_KEY', 'VIDEO_MODEL', 'MINIMAX_API_KEY')


def config(role, **env):
    with patch.object(llm, 'load_env', return_value=env):
        return llm.model_config(role)


class ModelConfigTests(unittest.TestCase):
    def test_defaults_to_deepseek(self):
        self.assertEqual(config('script', DEEPSEEK_API_KEY='k'),
                         {'base_url': 'https://api.deepseek.com', 'api_key': 'k', 'model': 'deepseek-chat', 'provider': 'DeepSeek'})
        self.assertEqual(config('video', DEEPSEEK_API_KEY='k')['model'], 'deepseek-flash')

    def test_each_step_can_use_its_own_provider(self):
        env = dict(DEEPSEEK_API_KEY='ds', VIDEO_BASE_URL='https://dashscope.aliyuncs.com/compatible-mode/v1/', VIDEO_API_KEY='qw', VIDEO_MODEL='qwen-vl-plus')
        self.assertEqual(config('video', **env), {'base_url': 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'api_key': 'qw', 'model': 'qwen-vl-plus', 'provider': '通义千问'})
        self.assertEqual(config('script', **env)['provider'], 'DeepSeek')   # 写脚本不受影响

    def test_shared_setting_and_old_name_still_work(self):
        shared = config('script', LLM_BASE_URL='https://api.openai.com/v1', LLM_API_KEY='o', SCRIPT_MODEL='gpt-x')
        self.assertEqual((shared['provider'], shared['api_key'], shared['model']), ('OpenAI', 'o', 'gpt-x'))
        self.assertEqual(config('video', DEEPSEEK_API_KEY='k', DEEPSEEK_VIDEO_MODEL='old-name')['model'], 'old-name')
        self.assertEqual(config('video', DEEPSEEK_API_KEY='k', DEEPSEEK_VIDEO_MODEL='old-name', VIDEO_MODEL='new-name')['model'], 'new-name')

    def test_unknown_provider_shows_its_host(self):
        self.assertEqual(llm.provider_label('https://llm.example.org/v1'), 'llm.example.org')

    def test_only_deepseek_gets_the_thinking_switch(self):
        self.assertIn('thinking', llm.extra_params('https://api.deepseek.com'))
        self.assertEqual(llm.extra_params('https://api.minimaxi.com/v1'), {})

    def test_script_json_retries_without_json_mode(self):
        calls = []

        def post(url, headers, body, timeout=120):
            calls.append((url, dict(body)))
            if 'response_format' in body:
                raise RuntimeError("400：unknown response_format type 'json_object'")
            return {'choices': [{'message': {'content': '```json\n{"lines": []}\n```'}}], 'usage': {'total_tokens': 5}}
        env = dict(SCRIPT_BASE_URL='https://api.minimaxi.com/v1', SCRIPT_API_KEY='m', SCRIPT_MODEL='MiniMax-Text-01')
        with patch.object(llm, 'load_env', return_value=env), patch.object(llm, '_post', side_effect=post):
            data, usage = llm.script_json('s', 'u')
        self.assertEqual(data, {'lines': []})
        self.assertEqual([('response_format' in body) for _, body in calls], [True, False])
        self.assertEqual(calls[0][0], 'https://api.minimaxi.com/v1/chat/completions')
        self.assertEqual((usage['provider'], usage['model']), ('MiniMax', 'MiniMax-Text-01'))


class DistillTests(unittest.TestCase):
    def test_find_cuts_ignores_shaky_footage(self):
        calm = [1.0] * 30
        calm[14] = 40.0                      # 平稳画面里突然一跳：切镜头
        self.assertEqual(ds.find_cuts(calm, fps=15), [1.0])
        shaky = [15.0, 18.0, 16.0, 20.0, 17.0, 19.0, 16.0, 18.0] * 4   # 镜头一直在晃：不算切
        self.assertEqual(ds.find_cuts(shaky, fps=15), [])

    def test_shot_lengths_merge_cuts_that_are_too_close(self):
        self.assertEqual(ds.shot_lengths([2.2, 4.4, 4.5, 7.0], 10.0), [2.2, 2.2, 2.6, 3.0])
        self.assertEqual(ds.shot_lengths([9.9], 10.0), [10.0])   # 离片尾太近的不算
        self.assertEqual(ds.shot_lengths([], 5.0), [5.0])

    def test_speech_stats(self):
        segments = [{'start': 0.0, 'end': 4.0, 'text': 'The Matterhorn is one of the most iconic peaks.'},
                    {'start': 4.0, 'end': 8.0, 'text': 'Search UK20 in the app for an extra 20% off.'}]
        stats = ds.speech_stats(segments, 9.0)
        self.assertEqual((stats['sentences'], stats['words_per_minute'], stats['max_words_per_sentence']), (2, 142, 10))
        self.assertFalse(stats['price_in_first_3s'])
        self.assertTrue(stats['cta_at_end'])
        self.assertEqual(stats['cta_seconds_before_end'], 5.0)
        early = ds.speech_stats([{'start': 0.0, 'end': 3.0, 'text': 'Save 30% on trains today.'}], 3.0)
        self.assertTrue(early['price_in_first_3s'])

    def test_summary_and_comparison(self):
        videos = [{'file': 'a.mp4', 'duration': 20.0, 'shots': [2.0, 2.2, 2.4, 5.0],
                   'speech': {'words_per_minute': 150, 'max_words_per_sentence': 16, 'price_in_first_3s': True, 'cta_at_end': False}}]
        summary = ds.summarize(videos)
        self.assertEqual((summary['shots'], summary['shot_median_s'], summary['shot_max_s']), (4, 2.3, 5.0))
        skill = {'max_shot_s': 3.2, 'voice': {'wpm_range': [130, 190]}}
        verdicts = {row[0]: row[3] for row in ds.compare(summary, skill)}
        self.assertEqual(verdicts['语速'], '一致')
        self.assertIn('偏紧', verdicts['单个镜头最长'])
        self.assertEqual(verdicts['每句最多几个词'], '成片里有更长的句子')
        self.assertEqual(verdicts['开头 3 秒不提价格'], '有成片开头就提了价格')
        self.assertEqual(verdicts['结尾有行动引导'], '有成片结尾没有引导')

    def test_skill_file_has_sourced_rules_and_pipeline_params(self):
        with open(os.path.join(ds.ROOT, 'data', 'skill.json'), encoding='utf-8') as f:
            skill = json.load(f)
        self.assertGreaterEqual(len(skill['rules']), 10)
        self.assertTrue(all(rule['source'] and rule['rule'] and rule['group'] for rule in skill['rules']))
        for key in ('max_shot_s', 'clip_skip_head_s', 'opening_huazi_max_s', 'benefit_sticker_offset_s', 'cta_min_s', 'gap_between_lines_s', 'tail_s'):
            self.assertIn(key, skill)


if __name__ == '__main__':
    unittest.main()
