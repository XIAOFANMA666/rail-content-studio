import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import generate_tiktok_ideas as ideas


class DailyIdeasTest(unittest.TestCase):
    def test_requires_successful_load_of_all_three_skills(self):
        events = []
        for name in ideas.SKILLS:
            events.extend([
                {'type': 'tool_call', 'callId': name, 'tool': 'skill', 'input': {'name': name}},
                {'type': 'tool_result', 'callId': name, 'status': 'completed'},
            ])
        events.append({'type': 'final', 'sessionId': 'trial', 'reason': {'kind': 'completed'}, 'text': json.dumps([])})
        self.assertEqual(ideas.harness_output(events), ([], 'trial'))
        with self.assertRaises(ValueError):
            ideas.harness_output(events[2:])

    def test_original_url_is_taken_from_the_collected_source(self):
        source = {'id': '10', 'original': 'https://www.tiktok.com/@rail/video/10'}
        suggestion = {'source_id': '10', 'original': 'https://example.com', 'topic': '夜车攻略',
                      'audience': '旅行者', 'hook_en': 'Taking the sleeper?', 'hook_zh': '要坐卧铺吗？',
                      'adaptation': '将旅程改成订前核对', 'visuals': ['车站', '车窗'], 'checks': ['核对班次']}
        self.assertEqual(ideas.suggestions([suggestion], {'items': [source]})[0]['original'], source['original'])
        with self.assertRaises(ValueError):
            ideas.suggestions([dict(suggestion, source_id='99')], {'items': [source]})
