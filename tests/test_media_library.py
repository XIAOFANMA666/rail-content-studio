"""Offline invariants. Provider/model responses and video files here are synthetic fixtures."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import media_http
import media_library as ml


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        (self.root / "data").mkdir()
        (self.root / "data/library.json").write_text(json.dumps({"clips": [
            {"id": "m01", "file": "clips/m01.mp4", "title": "历史列车", "tags": ["train"],
             "places": [], "location": "未知", "quality": 3, "issues": [], "pexels": "https://www.pexels.com/video/1/"}]}))
        self.service = ml.Service(self.root, self.out)

    def seed(self, mid="px123", **changes):
        folder = self.out / "media/candidates" / mid
        folder.mkdir(parents=True)
        (folder / "source.mp4").write_bytes(b"synthetic-video-fixture")
        (folder / "frame-00.jpg").write_bytes(b"synthetic-frame")
        row = {"id": mid, "source": "Pexels", "source_id": mid[2:], "title": "雪山列车",
               "source_url": "https://www.pexels.com/video/123/", "author": "fixture author",
               "status": "review", "tags": ["train", "mountain"], "places": [], "quality": 3,
               "description": "列车经过山间", "issues": [],
               "candidate_file": (folder / "source.mp4").relative_to(self.root).as_posix(),
               "thumbnail": (folder / "frame-00.jpg").relative_to(self.root).as_posix(),
               "frames": [{"second": .5, "file": (folder / "frame-00.jpg").relative_to(self.root).as_posix()}],
               "technical": {"duration": 8, "width": 1920, "height": 1080, "fps": 30},
               "sha256": hashlib.sha256(b"synthetic-video-fixture").hexdigest(),
               "request": {"query": "train"}, "technical_warnings": [], "duplicate_ids": []}
        row.update(changes)
        with self.service.db() as db:
            db.execute("INSERT INTO media VALUES(?,?,?,?,?,?,?)",
                       (mid, "Pexels", mid[2:], row["status"], 1, ml.encode(row), ml.now()))
        return row

    def approval(self, mid="px123", **changes):
        body = {"id": mid, "revision": self.service.media(mid)["revision"], "action": "approve",
                "actor": "测试审批人", "note": "", "watched": True, "rights_confirmed": True,
                "rights_note": "测试授权记录，不代表真实授权", "tags": ["train", "mountain"],
                "places": [], "quality": 4}
        body.update(changes)
        return body

    def test_unapproved_candidate_is_not_recalled(self):
        self.seed()
        self.assertEqual(ml.approved_clips(self.root, self.out), [])
        self.assertEqual(len(self.service.listing({"status": "pending"})["items"]), 1)

    def test_approval_publishes_existing_files_and_audit_together(self):
        self.seed()
        result = self.service.decide(self.approval())
        clips = ml.approved_clips(self.root, self.out)
        self.assertEqual(result["status"], "approved")
        self.assertEqual(len(clips), 1)
        self.assertTrue((self.root / clips[0]["file"]).is_file())
        self.assertTrue((self.root / clips[0]["thumbnail"]).is_file())
        self.assertEqual(result["events"][0]["actor"], "测试审批人")

    def test_approval_requires_viewing_rights_and_place_evidence(self):
        self.seed()
        for change in ({"watched": False}, {"rights_confirmed": False}, {"rights_note": ""},
                       {"places": ["瑞士"], "place_evidence": ""}):
            with self.subTest(change=change), self.assertRaises(ml.MediaError):
                self.service.decide(self.approval(**change))
        self.assertEqual(self.service.media("px123")["status"], "review")
        self.assertEqual(ml.approved_clips(self.root, self.out), [])

    def test_stale_and_duplicate_approval_rejected(self):
        self.seed()
        body = self.approval()
        self.service.decide(body)
        with self.assertRaises(ml.MediaError):
            self.service.decide(body)
        self.assertEqual(len(self.service.media("px123")["events"]), 1)

    def test_missing_or_changed_file_never_enters_library(self):
        self.seed()
        source = self.root / self.service.media("px123")["candidate_file"]
        source.write_bytes(b"changed")
        with self.assertRaises(ml.MediaError):
            self.service.decide(self.approval())
        source.unlink()
        with self.assertRaises(ml.MediaError):
            self.service.decide(self.approval())
        self.assertEqual(ml.approved_clips(self.root, self.out), [])

    def test_risk_and_failed_analysis_require_explanation(self):
        self.seed(issues=["brand"], analysis_error="未执行")
        with self.assertRaises(ml.MediaError):
            self.service.decide(self.approval())
        result = self.service.decide(self.approval(note="测试：已人工逐帧核对并记录使用限制"))
        self.assertEqual(result["issues"], ["brand"])

    def test_disable_removes_from_recall_but_retains_file(self):
        self.seed()
        m = self.service.decide(self.approval())
        m = self.service.decide({"id": m["id"], "revision": m["revision"], "action": "disable",
                                 "actor": "测试人", "note": "测试停用"})
        self.assertEqual(ml.approved_clips(self.root, self.out), [])
        self.assertTrue((self.root / m["file"]).is_file())
        with self.assertRaises(ml.MediaError):
            media_http.validate_swaps(self.root, self.out, {}, {"0": {"0": m["id"]}})

    def test_disabled_asset_remains_available_only_in_existing_timeline(self):
        self.seed()
        self.service.decide(self.approval())
        clip = ml.approved_clips(self.root, self.out)[0]
        m = self.service.media(clip['id'])
        self.service.decide({'id': m['id'], 'revision': m['revision'], 'action': 'disable',
                             'actor': '测试人', 'note': '停用'})
        snapshot = {'segments': [{'shots': [{'clip': clip['id']}]}], 'clips': {clip['id']: clip}}
        materials = ml.redraft_materials(self.root, snapshot, {'clips': []})
        self.assertEqual(materials[0]['id'], clip['id'])
        unused = {'segments': [], 'clips': snapshot['clips']}
        self.assertEqual(ml.redraft_materials(self.root, unused, {'clips': []}), [])
        self.assertEqual(ml.approved_clips(self.root, self.out), [])

    def test_recall_excludes_material_shorter_than_required_shot(self):
        import pipeline
        clips = [{'id': 'short', 'title': 'short', 'tags': ['train'], 'places': [],
                  'location': '未知', 'quality': 5, 'issues': []},
                 {'id': 'long', 'title': 'long', 'tags': ['train'], 'places': [],
                  'location': '未知', 'quality': 3, 'issues': []}]
        _, _, candidates, excluded = pipeline.retrieve({'en': 'train', 'tags': ['train']},
                                                       clips, {'short': 1, 'long': 8}, 4)
        self.assertEqual([c['id'] for c in candidates], ['long'])
        self.assertIn('时长不足', excluded[0]['excluded'])

    def test_needs_info_and_reject_remain_outside_library(self):
        self.seed()
        m = self.service.decide({"id": "px123", "revision": 1, "action": "needs_info",
                                 "actor": "测试人", "note": "地点依据待补"})
        self.assertEqual(m["status"], "needs_info")
        self.service.decide({"id": "px123", "revision": m["revision"], "action": "reject",
                             "actor": "测试人", "note": "不匹配"})
        self.assertEqual(ml.approved_clips(self.root, self.out), [])
        self.assertEqual(self.service.listing({"status": "pending"})["total"], 0)

    def test_known_duplicate_cannot_be_approved(self):
        self.seed(duplicate_ids=["px999"])
        with self.assertRaises(ml.MediaError):
            self.service.decide(self.approval(note="仍想入库"))

    def test_duplicate_check_is_repeated_at_approval_time(self):
        self.seed("px123")
        self.seed("px124")
        self.service.decide(self.approval("px123"))
        with self.assertRaises(ml.MediaError):
            self.service.decide(self.approval("px124"))
        self.assertEqual(len(ml.approved_clips(self.root, self.out)), 1)

    def test_restart_does_not_replay_jobs(self):
        with self.service.db() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",
                       ("job", "search", "running", ml.encode({"request": {"query": "train"}}), ml.now()))
        restarted = ml.Service(self.root, self.out)
        self.assertEqual(restarted.task("job")["status"], "interrupted")
        self.assertIsNone(restarted.worker)

    def test_chinese_query_expands_and_legacy_has_no_fabricated_approval(self):
        rows = self.service.listing({"status": "approved", "q": "火车"})["items"]
        self.assertEqual(rows[0]["id"], "m01")
        self.assertIsNone(rows[0]["approval"])
        self.assertIn("train", rows[0]["match_terms"])

    def test_media_paths_and_provider_urls_are_restricted(self):
        for value in ("https://evil.test/video", "http://videos.pexels.com/a",
                      "https://api.pexels.com@evil.test/a", "https://videos.pexels.com:444/a"):
            with self.subTest(url=value), self.assertRaises(ml.MediaError):
                ml.safe_url(value)
        for path in ("../../.env", "approved/px123/../../media-library.sqlite3", "approved/px123/source.mp4/extra"):
            with self.subTest(path=path), self.assertRaises(ml.MediaError):
                self.service.file(path)

    def test_invalid_model_observations_are_not_success(self):
        frames = [{"second": 1}, {"second": 2}]
        with self.assertRaises(ml.MediaError):
            ml.validate_analysis({"observations": [], "risks": []}, frames, 3)
        data = {"observations": [{"second": 1, "description": "a"}, {"second": 2, "description": "b"}],
                "risks": [{"second": 2.8, "type": "brand", "evidence": "标识"}],
                "description": "a", "tags": ["train"], "suggested_places": [], "match": "符合", "reason": "b"}
        with self.assertRaises(ml.MediaError):
            ml.validate_analysis(data, frames, 3)

    def test_limits_reject_nan_and_invalid_shapes(self):
        for body in ({"query": ""}, {"query": "x", "min_width": float("nan")},
                     {"query": "x", "queries": ["a"] * 4}, {"query": "x", "orientation": "wrong"}):
            with self.subTest(body=body), self.assertRaises(ml.MediaError):
                ml.validate_request(body)

    def test_cancelled_task_cannot_be_overwritten_by_worker(self):
        with self.service.db() as db:
            db.execute("INSERT INTO tasks VALUES(?,?,?,?,?)",
                       ("job", "search", "running", ml.encode({"request": {"query": "train"}}), ml.now()))
        self.service.cancel("job")
        self.service.task_update("job", "done", progress="late update")
        self.assertEqual(self.service.task("job")["status"], "cancelled")


class StreamTests(unittest.TestCase):
    def test_range_streaming_and_invalid_range(self):
        class Handler:
            def __init__(self, header):
                self.headers = {"Range": header}
                self.wfile = io.BytesIO()
                self.sent = {}
            def send_response(self, status):
                self.status = status
            def send_header(self, key, value):
                self.sent[key] = value
            def end_headers(self):
                pass
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.mp4"
            path.write_bytes(b"0123456789")
            h = Handler("bytes=2-5")
            media_http.stream_file(h, path)
            self.assertEqual(h.status, 206)
            self.assertEqual(h.wfile.getvalue(), b"2345")
            h = Handler("bytes=99-")
            media_http.stream_file(h, path)
            self.assertEqual(h.status, 416)
            self.assertEqual(h.wfile.getvalue(), b"")


if __name__ == "__main__":
    unittest.main()
