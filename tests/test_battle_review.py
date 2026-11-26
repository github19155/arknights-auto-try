"""battle_review 抽帧接口契约测试：全部离线，使用合成短视频与临时事件文件。"""
from __future__ import annotations

import json
import math
import struct
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from src.engine.battle_review import (
    FULL_FRAME_MAX_WINDOW_S,
    MAX_FRAMES_PER_REQUEST,
    extract_review_frames,
)

FPS = 30
WIDTH, HEIGHT = 64, 48
FRAMES = 90  # 3.0s

# 复现手机录屏这类 VFR 录像：帧间隔忽长忽短，平均 FPS 与真实 PTS 不符。
VFR_TIMES_S = [0.0, 0.04, 0.25, 0.7, 1.2, 1.21, 1.9, 2.6]
VFR_LEVELS = [10 + 20 * index for index in range(len(VFR_TIMES_S))]


def make_video(path: Path, frames: int = FRAMES, fps: int = FPS) -> None:
    writer = cv2.VideoWriter(
        str(path), cv2.VideoWriter_fourcc(*"mp4v"), float(fps), (WIDTH, HEIGHT)
    )
    if not writer.isOpened():
        raise RuntimeError("本机 OpenCV 无法创建 mp4v 测试录像")
    for i in range(frames):
        image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        image[:, :] = (i % 256, (i * 3) % 256, 200)
        writer.write(image)
    writer.release()


def _ebml_size(value: int) -> bytes:
    for length in range(1, 9):
        if value < (1 << (7 * length)) - 1:
            payload = value.to_bytes(length, "big")
            return bytes([payload[0] | (1 << (8 - length))]) + payload[1:]
    raise ValueError(f"EBML 尺寸过大：{value}")


def _ebml(element_id: bytes, payload: bytes) -> bytes:
    return element_id + _ebml_size(len(payload)) + payload


def _ebml_uint(value: int) -> bytes:
    return value.to_bytes(max((value.bit_length() + 7) // 8, 1), "big")


def make_vfr_video(path: Path, times_s: list[float], levels: list[int]) -> None:
    """手写最小 Matroska（V_MJPEG + 每块显式时间戳）复现 VFR。

    OpenCV 的 VideoWriter 只能写固定帧率，因此这里直接构造 SimpleBlock
    时间戳；每帧填唯一灰度，便于断言“时间戳与图片来自同一帧”。
    """
    last_ms = round(times_s[-1] * 1000)
    header = _ebml(b"\x1a\x45\xdf\xa3",
                   _ebml(b"\x42\x86", _ebml_uint(1))
                   + _ebml(b"\x42\xf7", _ebml_uint(1))
                   + _ebml(b"\x42\xf2", _ebml_uint(4))
                   + _ebml(b"\x42\xf3", _ebml_uint(2))
                   + _ebml(b"\x42\x82", b"matroska")
                   + _ebml(b"\x42\x87", _ebml_uint(4))
                   + _ebml(b"\x42\x85", _ebml_uint(2)))
    info = _ebml(b"\x15\x49\xa9\x66",
                 _ebml(b"\x2a\xd7\xb1", _ebml_uint(1_000_000))  # TimecodeScale = 1ms
                 + _ebml(b"\x44\x89", struct.pack(">d", float(last_ms)))
                 + _ebml(b"\x4d\x80", b"battle-review-test")
                 + _ebml(b"\x57\x41", b"battle-review-test"))
    track = _ebml(b"\xae",
                  _ebml(b"\xd7", _ebml_uint(1))
                  + _ebml(b"\x73\xc5", _ebml_uint(1))
                  + _ebml(b"\x83", _ebml_uint(1))
                  + _ebml(b"\x9c", _ebml_uint(0))
                  + _ebml(b"\x86", b"V_MJPEG")
                  + _ebml(b"\xe0", _ebml(b"\xb0", _ebml_uint(WIDTH))
                          + _ebml(b"\xba", _ebml_uint(HEIGHT))))
    blocks = b""
    for seconds, level in zip(times_s, levels):
        image = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
        image[:, :] = (level, level, level)
        ok, encoded = cv2.imencode(".jpg", image,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 95])
        if not ok:
            raise RuntimeError("无法编码 VFR 测试帧")
        blocks += _ebml(b"\xa3",
                        b"\x81" + struct.pack(">h", round(seconds * 1000))
                        + b"\x80" + encoded.tobytes())
    cluster = _ebml(b"\x1f\x43\xb6\x75", _ebml(b"\xe7", _ebml_uint(0)) + blocks)
    path.write_bytes(header + _ebml(b"\x18\x53\x80\x67",
                                    info + _ebml(b"\x16\x54\xae\x6b", track)
                                    + cluster))


def frame_level(path: str) -> int:
    image = cv2.imread(path)
    if image is None:
        raise AssertionError(f"PNG 不可读：{path}")
    return int(round(float(image[:, :, 0].mean())))


def write_events(path: Path, actions: list[dict]) -> None:
    lines = []
    for index, action in enumerate(actions):
        payload = {
            "what": "CopilotAction",
            "details": {
                "action": action["action"],
                "target": action.get("target", ""),
                "elapsed_time": action.get("elapsed_time", -1),
                "action_index": action.get("action_index", index),
            },
        }
        lines.append(json.dumps({"message": 2, "details": json.dumps(payload, ensure_ascii=False)},
                                ensure_ascii=False))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class ReviewTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.run_dir = Path(self._tmp.name)
        self.video = self.run_dir / "battle.mp4"
        make_video(self.video)

    def manifest(self, **anchors) -> None:
        (self.run_dir / "review-manifest.json").write_text(
            json.dumps(anchors, ensure_ascii=False), encoding="utf-8"
        )

    def frame_time(self, index: int) -> float:
        return round(index / FPS, 3)


class SinglePointTest(ReviewTestCase):
    def test_nearest_frame_and_delta_without_manifest(self) -> None:
        result = extract_review_frames(str(self.run_dir), start_s=1.02)

        self.assertTrue(result["ok"])
        self.assertEqual(result["time_base"], "battle")
        self.assertEqual(result["run_dir"], str(self.run_dir))
        self.assertEqual(len(result["frames"]), 1)
        frame = result["frames"][0]
        expected_index = round(1.02 * FPS)
        self.assertEqual(frame["actual_s"], self.frame_time(expected_index))
        self.assertAlmostEqual(frame["requested_s"], 1.02, places=6)
        self.assertAlmostEqual(
            frame["request_delta_s"], expected_index / FPS - 1.02, places=6
        )
        self.assertTrue(Path(frame["frame_path"]).is_file())
        self.assertIsNone(frame["leak_relative_s"])
        self.assertIsNone(frame["nearest_action"])
        self.assertTrue(any("review-manifest.json 缺失" in w for w in result["warnings"]))

    def test_manifest_battle_anchor_offsets_video_time(self) -> None:
        self.manifest(battle_start=1.0)
        result = extract_review_frames(str(self.run_dir), start_s=0.5)

        self.assertAlmostEqual(result["requested"]["video_start_s"], 1.5, places=6)
        self.assertAlmostEqual(result["frames"][0]["battle_s"], 0.5, places=3)
        self.assertAlmostEqual(result["frames"][0]["actual_s"], 1.5, places=3)


class RangeIntervalTest(ReviewTestCase):
    def test_interval_sampling_includes_endpoints(self) -> None:
        result = extract_review_frames(
            str(self.run_dir), start_s=0.2, end_s=1.0, interval_s=0.4
        )

        frames = result["frames"]
        self.assertEqual([f["requested_s"] for f in frames], [0.2, 0.6, 1.0])
        for frame in frames:
            self.assertLessEqual(abs(frame["request_delta_s"]), 0.5 / FPS + 1e-6)
        self.assertEqual(result["requested"]["interval_s"], 0.4)

    def test_duplicate_frames_are_deduplicated(self) -> None:
        result = extract_review_frames(
            str(self.run_dir), start_s=0.0, end_s=0.1, interval_s=0.001
        )

        actual = [f["actual_s"] for f in result["frames"]]
        self.assertEqual(len(actual), len(set(actual)))
        self.assertLessEqual(len(actual), MAX_FRAMES_PER_REQUEST)


class LeakTimeBaseTest(ReviewTestCase):
    def test_leak_relative_times_use_first_leak_anchor(self) -> None:
        self.manifest(battle_start=0.5, leak_time=2.0)
        result = extract_review_frames(
            str(self.run_dir), start_s=-0.5, end_s=0.0, interval_s=0.5, time_base="leak"
        )

        frames = result["frames"]
        self.assertEqual([f["leak_relative_s"] for f in frames], [-0.5, 0.0])
        self.assertEqual([f["battle_s"] for f in frames], [1.0, 1.5])
        self.assertEqual(result["time_base"], "leak")

    def test_leak_without_anchor_is_explicit_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            extract_review_frames(str(self.run_dir), start_s=-0.5, time_base="leak")
        self.assertIn("锚点", str(caught.exception))

    def test_leak_anchor_can_be_battle_relative(self) -> None:
        self.manifest(battle_start=1.0, leak_battle_s=0.5)
        result = extract_review_frames(
            str(self.run_dir), start_s=0.0, time_base="leak"
        )
        self.assertAlmostEqual(result["frames"][0]["leak_relative_s"], 0.0, places=3)
        self.assertAlmostEqual(result["frames"][0]["actual_s"], 1.5, places=3)


class NearestActionTest(ReviewTestCase):
    def test_picks_closest_timed_copilot_action(self) -> None:
        write_events(self.run_dir / "maa-events.jsonl", [
            {"action": "Deploy", "target": "甲", "elapsed_time": 1.0, "action_index": 0},
            {"action": "Skill", "target": "", "elapsed_time": 2.0, "action_index": 1},
        ])
        result = extract_review_frames(str(self.run_dir), start_s=1.4)

        action = result["frames"][0]["nearest_action"]
        self.assertEqual(action["action_index"], 0)
        self.assertIn("Deploy", action["action"])
        self.assertIn("甲", action["action"])
        self.assertAlmostEqual(action["battle_s"], 1.0, places=3)
        self.assertAlmostEqual(action["delta_s"], 0.4, places=3)

    def test_untimed_actions_warn_instead_of_faking_nearest(self) -> None:
        write_events(self.run_dir / "maa-events.jsonl", [
            {"action": "Deploy", "target": "乙", "elapsed_time": -1},
        ])
        result = extract_review_frames(str(self.run_dir), start_s=0.5)

        self.assertIsNone(result["frames"][0]["nearest_action"])
        self.assertTrue(any("elapsed_time" in w for w in result["warnings"]))


class FullFrameModeTest(ReviewTestCase):
    def test_all_frames_in_small_window(self) -> None:
        result = extract_review_frames(
            str(self.run_dir), start_s=0.1, end_s=0.2, interval_s=None
        )

        frames = result["frames"]
        self.assertEqual(len(frames), 4)
        self.assertEqual([f["actual_s"] for f in frames],
                         [self.frame_time(i) for i in range(3, 7)])
        self.assertIsNone(result["requested"]["interval_s"])

    def test_default_limit_rejects_long_full_frame_window(self) -> None:
        with self.assertRaises(ValueError) as caught:
            extract_review_frames(
                str(self.run_dir), start_s=0.0,
                end_s=FULL_FRAME_MAX_WINDOW_S + 0.1, interval_s=None,
            )
        self.assertIn("interval_s", str(caught.exception))

    def test_default_limit_rejects_too_many_sampled_frames(self) -> None:
        with self.assertRaises(ValueError) as caught:
            extract_review_frames(
                str(self.run_dir), start_s=0.0, end_s=2.0, interval_s=0.01
            )
        self.assertIn(str(MAX_FRAMES_PER_REQUEST), str(caught.exception))

    def test_full_frame_boundary_at_video_end(self) -> None:
        result = extract_review_frames(
            str(self.run_dir), start_s=2.9, end_s=3.0, interval_s=None
        )
        self.assertTrue(result["frames"])
        self.assertLessEqual(result["frames"][-1]["actual_s"], 3.0)


class VfrReviewTestCase(ReviewTestCase):
    """把 battle.mp4 换成 VFR 素材；真实 PTS 与平均 FPS 换算会给出不同结果。"""

    def setUp(self) -> None:
        super().setUp()
        make_vfr_video(self.video, VFR_TIMES_S, VFR_LEVELS)

    def assertFrameIs(self, frame: dict, level: int) -> None:
        self.assertLessEqual(abs(frame_level(frame["frame_path"]) - level), 2)


class VfrTimestampTest(VfrReviewTestCase):
    def test_single_point_uses_container_pts(self) -> None:
        result = extract_review_frames(str(self.run_dir), start_s=1.15)

        self.assertEqual(len(result["frames"]), 1)
        frame = result["frames"][0]
        self.assertAlmostEqual(frame["actual_s"], 1.2, places=3)
        self.assertAlmostEqual(frame["requested_s"], 1.15, places=6)
        self.assertAlmostEqual(frame["request_delta_s"], 0.05, places=6)
        self.assertAlmostEqual(frame["battle_s"], 1.2, places=3)
        self.assertFrameIs(frame, VFR_LEVELS[4])

    def test_interval_sampling_picks_nearest_real_pts(self) -> None:
        result = extract_review_frames(
            str(self.run_dir), start_s=0.1, end_s=1.3, interval_s=0.6
        )

        frames = result["frames"]
        self.assertEqual([f["actual_s"] for f in frames], [0.04, 0.7, 1.21])
        self.assertAlmostEqual(frames[0]["request_delta_s"], -0.06, places=6)
        self.assertAlmostEqual(frames[1]["request_delta_s"], 0.0, places=6)
        self.assertAlmostEqual(frames[2]["request_delta_s"], -0.09, places=6)
        for frame, index in zip(frames, (1, 3, 5)):
            self.assertFrameIs(frame, VFR_LEVELS[index])

    def test_full_frame_window_filters_on_real_pts(self) -> None:
        result = extract_review_frames(
            str(self.run_dir), start_s=1.0, end_s=1.25, interval_s=None
        )

        frames = result["frames"]
        self.assertEqual([f["actual_s"] for f in frames], [1.2, 1.21])
        self.assertEqual([f["request_delta_s"] for f in frames], [0.0, 0.0])
        self.assertFrameIs(frames[0], VFR_LEVELS[4])
        self.assertFrameIs(frames[1], VFR_LEVELS[5])

    def test_full_frame_window_returns_every_vfr_frame(self) -> None:
        result = extract_review_frames(
            str(self.run_dir), start_s=0.0, end_s=1.3, interval_s=None
        )

        frames = result["frames"]
        self.assertEqual([f["actual_s"] for f in frames],
                         [0.0, 0.04, 0.25, 0.7, 1.2, 1.21])
        for frame, level in zip(frames, VFR_LEVELS):
            self.assertFrameIs(frame, level)

    def test_leak_relative_seconds_use_real_pts(self) -> None:
        self.manifest(battle_start=0.5, leak_time=1.0)
        result = extract_review_frames(
            str(self.run_dir), start_s=0.1, time_base="leak", interval_s=None,
            end_s=0.21,
        )

        frames = result["frames"]
        self.assertEqual([f["actual_s"] for f in frames], [1.2, 1.21])
        self.assertEqual([f["leak_relative_s"] for f in frames], [0.2, 0.21])
        self.assertEqual([f["battle_s"] for f in frames], [0.7, 0.71])
        self.assertFrameIs(frames[0], VFR_LEVELS[4])
        self.assertFrameIs(frames[1], VFR_LEVELS[5])


class ErrorTest(ReviewTestCase):
    def test_start_beyond_video_raises(self) -> None:
        with self.assertRaises(ValueError):
            extract_review_frames(str(self.run_dir), start_s=5.0)

    def test_start_after_end_raises(self) -> None:
        with self.assertRaises(ValueError):
            extract_review_frames(str(self.run_dir), start_s=2.0, end_s=1.0)

    def test_negative_battle_start_raises(self) -> None:
        with self.assertRaises(ValueError):
            extract_review_frames(str(self.run_dir), start_s=-0.5)

    def test_negative_interval_raises(self) -> None:
        with self.assertRaises(ValueError):
            extract_review_frames(str(self.run_dir), start_s=0.0, end_s=1.0, interval_s=-0.5)

    def test_invalid_time_base_raises(self) -> None:
        with self.assertRaises(ValueError):
            extract_review_frames(str(self.run_dir), start_s=0.5, time_base="wall")

    def test_missing_video_raises(self) -> None:
        self.video.unlink()
        with self.assertRaises(ValueError):
            extract_review_frames(str(self.run_dir), start_s=0.5)

    def test_unreadable_video_raises(self) -> None:
        self.video.write_bytes(b"not a video at all")
        with self.assertRaises(RuntimeError):
            extract_review_frames(str(self.run_dir), start_s=0.5)

    def test_non_finite_start_raises(self) -> None:
        with self.assertRaises(ValueError):
            extract_review_frames(str(self.run_dir), start_s=math.nan)


class OutputLayoutTest(ReviewTestCase):
    def test_frames_land_in_unique_subdir_under_review_frames(self) -> None:
        first = extract_review_frames(str(self.run_dir), start_s=0.5)
        second = extract_review_frames(str(self.run_dir), start_s=0.5)

        first_dir = Path(first["frame_dir"])
        second_dir = Path(second["frame_dir"])
        self.assertEqual(first_dir.parent, self.run_dir / "review_frames")
        self.assertNotEqual(first_dir, second_dir)
        self.assertTrue(first_dir.is_dir())
        for frame in first["frames"]:
            self.assertEqual(Path(frame["frame_path"]).parent, first_dir)
            self.assertIsNotNone(cv2.imread(frame["frame_path"]))


if __name__ == "__main__":
    unittest.main()
