"""草稿计算项的离线测试：每一项各测满分、部分分、0 分。"""
import copy
import unittest

import draft_scoring as ds

SKILL = {'max_shot_s': 3.2, 'cta_min_s': 1.5, 'subtitle': {'max_chars_per_line': 42}}


def shot(start, dur, clip='m01', excluded=None):
    return {'clip': clip, 'start': start, 'dur': dur, 'excluded': excluded}


def make():
    """两句口播、四个镜头的小粗剪，所有项目都满分。"""
    return {
        'total_s': 10.0, 'skill': copy.deepcopy(SKILL),
        'scripts': {'chosen': 'A', 'variants': [{'id': 'A', 'ok': True, 'claims': []}, {'id': 'B', 'ok': False, 'claims': ['x']}]},
        'segments': [
            {'start': 0.0, 'dur': 5.0, 'subtitle': 'Trains in Switzerland offer\npanoramic windows.',
             'shots': [shot(0.0, 2.5), shot(2.5, 2.5)], 'stickers': []},
            {'start': 5.0, 'dur': 5.0, 'subtitle': 'Search UK20 in the app.',
             'shots': [shot(5.0, 2.5), shot(7.5, 2.5)], 'stickers': [{'kind': '结尾钩子', 'text': 'UK20', 'start': 5.2, 'dur': 4.8}]},
        ],
    }


TRANSCRIPT = 'Trains in Switzerland offer panoramic windows. Search UK 20 in the app.'


class MatchTests(unittest.TestCase):
    def test_same_video_within_half_a_second(self):
        self.assertEqual(ds.matches(make(), 10.3), (True, 0.3))
        self.assertEqual(ds.matches(make(), 11.0), (False, 1.0))
        self.assertEqual(ds.matches({}, 10.0), (False, None))


class ItemTests(unittest.TestCase):
    def test_all_full_marks(self):
        scored, claims = ds.score_all(make(), TRANSCRIPT)
        self.assertEqual({k: v['score'] for k, v in scored.items()},
                         {'editing_pace': 6, 'editing_sync': 5, 'editing_complete': 4, 'visual_location': 5,
                          'content_fact': 6, 'subtitle_correct': 4, 'subtitle_readable': 3, 'cta_clear': 4})
        self.assertEqual(claims, [])
        self.assertTrue(all(v['reason'] for v in scored.values()))

    def test_editing_pace(self):
        r = make()
        r['segments'][0]['shots'] = [shot(0.0, 0.8), shot(0.8, 4.2)]
        self.assertEqual(ds.editing_pace(r, SKILL)[0], 3)
        r['segments'][1]['shots'] = [shot(5.0, 0.5), shot(5.5, 4.5)]
        self.assertEqual(ds.editing_pace(r, SKILL)[0], 0)
        self.assertIsNone(ds.editing_pace(r, {})[0])

    def test_editing_sync(self):
        r = make()
        r['segments'].append({'start': 10.0, 'dur': 2.0, 'subtitle': 'Go.', 'shots': [shot(10.6, 1.4)], 'stickers': []})
        self.assertEqual(ds.editing_sync(r)[0], 2)     # 两处换句，只有一处有切点
        r['segments'][1]['shots'] = [shot(5.6, 1.9), shot(7.5, 2.5)]
        self.assertEqual(ds.editing_sync(r)[0], 0)

    def test_editing_complete(self):
        self.assertEqual(ds.editing_complete(True, True)[0], 4)
        self.assertEqual(ds.editing_complete(True, False)[0], 0)

    def test_visual_location(self):
        r = make()
        r['segments'][0]['shots'][1] = shot(2.5, 2.5, 'm25', '地点不符：斯特拉斯堡')
        score, reason = ds.visual_location(r)
        self.assertEqual(score, 0)
        self.assertIn('m25', reason)

    def test_content_fact(self):
        r = make()
        r['scripts']['variants'][0]['claims'] = ['伦敦到爱丁堡 4 小时']
        self.assertEqual(ds.content_fact(r), (3, '脚本通过审核，但有 1 处说法在事实库里查不到，要人核对。', ['伦敦到爱丁堡 4 小时']))
        r['scripts']['chosen'] = 'B'
        self.assertEqual(ds.content_fact(r)[0], 0)
        r['scripts']['chosen'] = 'Z'
        self.assertIsNone(ds.content_fact(r)[0])

    def test_subtitle_correct(self):
        self.assertGreaterEqual(ds.similarity('Search UK20 in the app', 'Search UK 20 in the app.'), 0.92)
        r = make()
        self.assertEqual(ds.subtitle_correct(r, 'Trains in Sweden offer panoramic windows. Search the app.')[0], 2)   # 相似度 86%
        self.assertEqual(ds.subtitle_correct(r, 'Completely different words about cooking pasta at home tonight.')[0], 0)
        self.assertIsNone(ds.subtitle_correct(r, '')[0])

    def test_long_text_is_not_treated_as_junk(self):
        text = 'Split ticketing can save you up to thirty percent on train travel across the country. ' * 6
        self.assertEqual(ds.similarity(text, text), 1.0)

    def test_subtitle_readable(self):
        r = make()
        r['segments'][0]['subtitle'] = 'This single subtitle line is far too long to read on a phone'
        self.assertEqual(ds.subtitle_readable(r, SKILL)[0], 1.5)
        r['segments'][0]['subtitle'] = 'one\ntwo\nthree'
        self.assertEqual(ds.subtitle_readable(r, SKILL)[0], 1.5)

    def test_cta_clear(self):
        r = make()
        r['segments'][1]['stickers'][0]['dur'] = 1.0
        self.assertEqual(ds.cta_clear(r, SKILL)[0], 2)
        r['segments'][1]['stickers'] = [{'kind': '利益点贴纸', 'text': 'SAVE', 'start': 5, 'dur': 3}]
        self.assertEqual(ds.cta_clear(r, SKILL)[0], 0)

    def test_broken_data_only_affects_its_own_item(self):
        r = make()
        r['segments'][0]['shots'][0] = {'clip': 'm01'}   # 缺 start 和 dur
        scored, _ = ds.score_all(r, TRANSCRIPT)
        self.assertIsNone(scored['editing_pace']['score'])
        self.assertIsNone(scored['editing_sync']['score'])
        self.assertEqual(scored['subtitle_correct']['score'], 4)
        self.assertEqual(scored['cta_clear']['score'], 4)


if __name__ == '__main__':
    unittest.main()
