"""runner 时间锚点与 CopilotAction 时间戳补齐的离线契约测试。

全部使用 fake clock 与合成短视频，不连接模拟器、不启动 MAA。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from src.engine.battle_review import extract_review_frames
from src.engine.runner import (
    REVIEW_MANIFEST_NAME,
    ReviewTimeline,
    _build_review_manifest,
    _fill_elapsed_time,
)

FPS = 30
WIDTH, HEIGHT = 64, 48
FRAMES = 90  # 3.0s


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_video(path: Path) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), float(FPS), (WIDTH, HEIGHT)
    )
    if not writer.isOpened():
        raise RuntimeError("本机 OpenCV 无法创建 mp4v 测试录像")
    for index in range(FRAMES):
        image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        image[:, :] = (index % 256, (index * 3) % 256, 200)
        writer.write(image)
    writer.release()


def action_payload(elapsed_time=-1, action: str = "Deploy",
                   target: str = "甲") -> dict:
    return {
        "what": "CopilotAction",
        "class": "asst::BattleProcessTask",
        "details": {
            "action": action,
            "target": target,
            "elapsed_time": elapsed_time,
        },
    }


def write_event_line(path: Path, payload: dict) -> None:
    """与 runner._callback 相同的落盘结构：details 是序列化后的 JSON 字符串。"""
    line = json.dumps({"message": 20003,
                       "details": json.dumps(payload, ensure_ascii=False)},
                      ensure_ascii=False)
    path.write_text(line + "\n", encoding="utf-8")


class TimelineConversionTest(unittest.TestCase):
    def test_battle_start_and_leak_time_use_video_seconds(self) -> None:
        clock = FakeClock(100.0)
        timeline = ReviewTimeline(clock)
        timeline.mark_video_start()
        clock.now = 101.5  # 录屏启动后的 1.5s 准备时间
        timeline.mark_battle_start()
        clock.now = 130.0
        timeline.mark_leak()

        self.assertAlmostEqual(timeline.battle_start_video_s(), 1.5)
        self.assertAlmostEqual(timeline.leak_battle_s(), 28.5)
        self.assertAlmostEqual(timeline.leak_video_s(), 30.0)
        self.assertAlmostEqual(timeline.battle_s(), 28.5)

        manifest = _build_review_manifest(
            timeline, video_started=True, video_available=True
        )
        self.assertAlmostEqual(manifest["battle_start"], 1.5, places=3)
        self.assertAlmostEqual(manifest["leak_time"], 30.0, places=3)
        self.assertAlmostEqual(manifest["leak_battle_s"], 28.5, places=3)
        self.assertTrue(manifest["leak_detected"])
        self.assertEqual(manifest["video"]["path"], "battle.mp4")
        self.assertTrue(manifest["video"]["available"])

    def test_no_leak_manifest_has_no_leak_time(self) -> None:
        clock = FakeClock(0.0)
        timeline = ReviewTimeline(clock)
        timeline.mark_video_start()
        clock.now = 0.5
        timeline.mark_battle_start()
        clock.now = 2.0

        manifest = _build_review_manifest(
            timeline, video_started=True, video_available=True
        )
        self.assertIsNone(manifest.get("leak_time"))
        self.assertFalse(manifest["leak_detected"])
        self.assertIsNone(manifest.get("leak_battle_s"))
        self.assertAlmostEqual(manifest["battle_start"], 0.5, places=3)
        self.assertTrue(any("leak_time" in item for item in manifest["warnings"]))


class NoVideoManifestTest(unittest.TestCase):
    def test_without_recording_no_video_anchor_is_fabricated(self) -> None:
        clock = FakeClock(10.0)
        timeline = ReviewTimeline(clock)
        timeline.mark_battle_start()
        clock.now = 20.0
        timeline.mark_leak()

        manifest = _build_review_manifest(
            timeline, video_started=False, video_available=False
        )
        self.assertIsNone(manifest["video"]["path"])
        self.assertFalse(manifest["video"]["available"])
        self.assertFalse(manifest["video"]["recorded"])
        self.assertIsNone(manifest.get("battle_start"))
        self.assertIsNone(manifest.get("leak_time"))
        self.assertAlmostEqual(manifest["leak_battle_s"], 10.0, places=3)
        self.assertTrue(any("未开启录屏" in item for item in manifest["warnings"]))

    def test_pull_failure_keeps_warning_and_no_path(self) -> None:
        clock = FakeClock(0.0)
        timeline = ReviewTimeline(clock)
        timeline.mark_video_start()
        clock.now = 1.0
        timeline.mark_battle_start()

        manifest = _build_review_manifest(
            timeline, video_started=True, video_available=False,
            warnings=["录屏拉取失败：empty file"],
        )
        self.assertIsNone(manifest["video"]["path"])
        self.assertIsNone(manifest.get("battle_start"))
        self.assertTrue(any("录屏拉取失败" in item for item in manifest["warnings"]))
        self.assertTrue(any("未拉取到可用" in item for item in manifest["warnings"]))


class CopilotActionElapsedTest(unittest.TestCase):
    def test_minus_one_is_replaced_with_battle_seconds(self) -> None:
        clock = FakeClock(100.0)
        timeline = ReviewTimeline(clock)
        timeline.mark_battle_start()
        clock.now = 103.25
        payload = action_payload(elapsed_time=-1)

        self.assertTrue(_fill_elapsed_time(payload, timeline.battle_s()))
        self.assertAlmostEqual(payload["details"]["elapsed_time"], 3.25, places=3)

    def test_missing_elapsed_time_is_filled(self) -> None:
        payload = action_payload()
        del payload["details"]["elapsed_time"]

        self.assertTrue(_fill_elapsed_time(payload, 2.0))
        self.assertEqual(payload["details"]["elapsed_time"], 2.0)

    def test_valid_native_elapsed_time_is_preserved(self) -> None:
        payload = action_payload(elapsed_time=1.75)

        self.assertFalse(_fill_elapsed_time(payload, 99.0))
        self.assertEqual(payload["details"]["elapsed_time"], 1.75)

    def test_without_battle_anchor_keeps_unusable_value(self) -> None:
        payload = action_payload(elapsed_time=-1)

        self.assertFalse(_fill_elapsed_time(payload, None))
        self.assertEqual(payload["details"]["elapsed_time"], -1)


class BattleReviewCompatibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        make_video(self.run_dir / "battle.mp4")
        self.events_path = self.run_dir / "maa-events.jsonl"

    def write_manifest(self, timeline: ReviewTimeline,
                       *, leak: bool = False) -> dict:
        manifest = _build_review_manifest(
            timeline, video_started=True, video_available=True
        )
        (self.run_dir / REVIEW_MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        return manifest

    def test_repaired_event_is_queryable_and_matches_nearest_action(self) -> None:
        clock = FakeClock(0.0)
        timeline = ReviewTimeline(clock)
        timeline.mark_video_start()
        clock.now = 0.5
        timeline.mark_battle_start()
        clock.now = 1.5
        payload = action_payload(elapsed_time=-1)
        self.assertTrue(_fill_elapsed_time(payload, timeline.battle_s()))
        write_event_line(self.events_path, payload)
        manifest = self.write_manifest(timeline)

        self.assertAlmostEqual(manifest["battle_start"], 0.5, places=3)
        result = extract_review_frames(str(self.run_dir), start_s=1.0)

        frame = result["frames"][0]
        self.assertAlmostEqual(frame["battle_s"], 1.0, places=3)
        self.assertAlmostEqual(frame["actual_s"], 1.5, places=3)
        self.assertIsNotNone(frame["nearest_action"])
        self.assertEqual(frame["nearest_action"]["action"], "Deploy 甲")
        self.assertAlmostEqual(frame["nearest_action"]["battle_s"], 1.0, places=3)
        self.assertAlmostEqual(frame["nearest_action"]["delta_s"], 0.0, places=3)

    def test_leak_base_uses_manifest_leak_time(self) -> None:
        clock = FakeClock(0.0)
        timeline = ReviewTimeline(clock)
        timeline.mark_video_start()
        clock.now = 0.5
        timeline.mark_battle_start()
        clock.now = 2.0
        timeline.mark_leak()
        self.write_manifest(timeline, leak=True)

        result = extract_review_frames(
            str(self.run_dir), start_s=0.0, time_base="leak"
        )

        frame = result["frames"][0]
        self.assertAlmostEqual(frame["actual_s"], 2.0, places=3)
        self.assertAlmostEqual(frame["leak_relative_s"], 0.0, places=3)
        self.assertAlmostEqual(frame["battle_s"], 1.5, places=3)


if __name__ == "__main__":
    unittest.main()
