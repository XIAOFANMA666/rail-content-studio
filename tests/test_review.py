"""数据复盘的离线测试。前几条就是第六节里写的验收样例。"""
import unittest

import review

PUBLISHED = [
    {"work_id": "W01", "platform": "TikTok", "account": "@a", "script_id": "S01", "title": "卧铺冷知识", "genre": "冷知识"},
    {"work_id": "W02", "platform": "Instagram", "account": "@b", "script_id": "S01", "title": "卧铺冷知识", "genre": "冷知识"},
    {"work_id": "W03", "platform": "TikTok", "account": "@a", "script_id": "S02", "title": "山峰冷知识", "genre": "冷知识"},
    {"work_id": "W04", "platform": "TikTok", "account": "@a", "script_id": "S03", "title": "一日游路线", "genre": "路线推荐"},
]


def row(work, views, at, platform="TikTok", status="ok"):
    return {"work_id": work, "platform": platform, "views": "" if views is None else str(views), "fetched_at": at, "status": status}


def report(*imports):
    state = {}
    unmatched = []
    for rows in imports:
        unmatched += review.apply_report(state, rows, PUBLISHED)
    return {r["work_id"]: r for r in review.daily_rows(state, PUBLISHED)}, unmatched


class AcceptanceTests(unittest.TestCase):
    def test_same_work_900_then_1100_is_one_candidate_with_delta_200(self):
        day1, day2 = [row("W01", 900, "11-09 09:00")], [row("W01", 1100, "11-10 09:00")]
        rows, _ = report(day1, day2, day2)            # 第二天的表重复导入一次
        self.assertEqual((rows["W01"]["views"], rows["W01"]["delta"]), (1100, 200))
        self.assertEqual(rows["W01"]["tags"], ["值得继续测"])
        self.assertTrue(rows["W01"]["new_hit"])
        self.assertEqual(sum("值得继续测" in r["tags"] for r in rows.values()), 1)

    def test_two_different_works_600_and_500_are_not_added_together(self):
        rows, _ = report([row("W01", 600, "11-10 09:00"), row("W03", 500, "11-10 09:00")])
        self.assertEqual([rows[w]["tags"] for w in ("W01", "W03")], [[], []])

    def test_zero_views_is_not_a_failed_fetch(self):
        day1 = [row("W01", 0, "11-09 09:00"), row("W03", 240, "11-09 09:00")]
        day2 = [row("W01", 0, "11-10 09:00"), row("W03", None, "11-10 09:00", status="failed")]
        rows, _ = report(day1, day2)
        self.assertEqual(rows["W01"]["tags"], ["零播"])
        self.assertEqual(rows["W03"]["tags"], ["待补"])
        self.assertEqual((rows["W03"]["views"], rows["W03"]["fetched_at"]), (240, "11-09 09:00"))   # 保留上次的读数和时间
        self.assertIn("11-09 09:00", rows["W03"]["note"])

    def test_same_script_on_two_platforms_is_two_rows(self):
        rows, _ = report([row("W01", 1200, "11-10 09:00"), row("W02", 300, "11-10 09:00", platform="Instagram")])
        self.assertEqual((rows["W01"]["views"], rows["W02"]["views"]), (1200, 300))
        self.assertEqual(rows["W02"]["tags"], [])


class RuleTests(unittest.TestCase):
    def test_rows_that_match_no_published_work_are_listed(self):
        rows, unmatched = report([row("X99", 75, "11-10 09:00"), row("W01", 10, "11-10 09:00", platform="YouTube")])
        self.assertEqual([(u["work_id"], u["platform"]) for u in unmatched], [("X99", "TikTok"), ("W01", "YouTube")])
        self.assertEqual(rows["W01"]["tags"], ["待补"])      # 发布了但还没取到过数

    def test_older_report_does_not_overwrite_newer_reading(self):
        rows, _ = report([row("W01", 1100, "11-10 09:00")], [row("W01", 900, "11-09 09:00")])
        self.assertEqual(rows["W01"]["views"], 1100)
        self.assertIsNone(rows["W01"]["delta"])

    def test_pending_clears_when_the_number_arrives(self):
        rows, _ = report([row("W01", 240, "11-09 09:00")], [row("W01", None, "11-10 09:00", status="failed")], [row("W01", 300, "11-10 15:00")])
        self.assertEqual((rows["W01"]["views"], rows["W01"]["delta"], rows["W01"]["tags"]), (300, 60, []))

    def test_genre_rates_are_smoothed_and_small_samples_fall_back(self):
        rows, _ = report([row("W01", 1100, "t"), row("W02", 400, "t", platform="Instagram"), row("W03", 100, "t"), row("W04", 2000, "t")])
        genres, overall = review.genre_rates(list(rows.values()))
        self.assertEqual(overall, round((2 + 1) / (4 + 2), 3))
        self.assertEqual((genres["冷知识"]["works"], genres["冷知识"]["hits"], genres["冷知识"]["rate"]), (3, 1, 0.4))
        self.assertEqual(genres["路线推荐"]["rate"], overall)          # 只有 1 条，用整体的
        self.assertIn("样本太少", genres["路线推荐"]["note"])

    def test_pair_is_same_genre_and_platform_but_a_different_script(self):
        rows, _ = report([row("W01", 1100, "t"), row("W02", 90, "t", platform="Instagram"), row("W03", 170, "t"), row("W04", 50, "t")])
        high, low = review.pick_pair(list(rows.values()))
        self.assertEqual((high["work_id"], low["work_id"]), ("W01", "W03"))
        none_rows, _ = report([row("W04", 2000, "t")])
        self.assertIsNone(review.pick_pair(list(none_rows.values())))

    def test_retest_suggestion_needs_all_three_fields(self):
        high, low = {"title": "a", "views": 1100, "script_id": "S01"}, {"title": "b", "views": 170, "script_id": "S02"}
        usage = {"provider": "DeepSeek", "model": "deepseek-chat"}
        good = review.retest_suggestion(high, low, {}, lambda s, u: ({"difference": "开头是提问", "change": "把第一句改成提问", "metric": "3 秒留存"}, usage))
        self.assertEqual((good["change"], good["model"]), ("把第一句改成提问", "DeepSeek · deepseek-chat"))
        with self.assertRaises(ValueError):
            review.retest_suggestion(high, low, {}, lambda s, u: ({"difference": "x", "change": ""}, usage))


class TopicTests(unittest.TestCase):
    def test_review_rates_change_topic_scores(self):
        example = review.topic_scores({"冷知识": 0.062, "路线推荐": 0.048, "优惠券": 0.021, "热点": 0.035})
        measured = review.topic_scores({"冷知识": 0.4, "路线推荐": 0.167, "优惠券": 0.273, "热点": 0.273})
        score = lambda scored, tid: next(t["score"] for t in scored if t["id"] == tid)
        self.assertEqual(example[0]["id"], "T1")
        self.assertEqual(measured[0]["id"], "T1")
        self.assertLess(score(measured, "T3"), score(example, "T3"))     # 路线推荐类没出千播，分数下降
        self.assertGreater(score(measured, "T2"), score(example, "T2"))


if __name__ == "__main__":
    unittest.main()
