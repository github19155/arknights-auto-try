"""证据帧选择回归：合成战斗帧不得被结算过滤误杀，合成结算页仍须过滤。

素材是 tests/assets 里 640×360 的合成整帧（纯色块/渐变几何示意，无游戏原画、
字体与账号信息），不读取任何实机录像。
"""
from __future__ import annotations

import unittest
from pathlib import Path

from PIL import Image

from src.engine.recorder import _settlement_like, select_evidence_frames

ASSETS = Path(__file__).resolve().with_name("assets")
SNOW_FRAMES = ("battle-snow-1.png", "battle-snow-2.png")
RESULT_FRAMES = ("result-failed.png", "result-cleared.png")
BRIGHT_BATTLE_FRAME = "battle-operator-panel.png"


def load(name: str) -> Image.Image:
    return Image.open(ASSETS / name).convert("RGB")


class SnowMapEvidenceTest(unittest.TestCase):
    def test_synthetic_snow_map_frames_are_not_settlement(self) -> None:
        for name in SNOW_FRAMES:
            with self.subTest(frame=name):
                self.assertFalse(_settlement_like(load(name)))

    def test_synthetic_snow_map_frames_are_selectable(self) -> None:
        frames = [load(name) for name in SNOW_FRAMES]

        selected = select_evidence_frames(frames, need=len(frames), include_last=True)

        self.assertEqual(selected, frames)

    def test_bright_battle_frame_with_operator_panel_is_selectable(self) -> None:
        frame = load(BRIGHT_BATTLE_FRAME)

        self.assertFalse(_settlement_like(frame))
        self.assertEqual(select_evidence_frames([frame], need=1, include_last=True), [frame])


class SettlementEvidenceTest(unittest.TestCase):
    def test_result_frames_are_settlement(self) -> None:
        for name in RESULT_FRAMES:
            with self.subTest(frame=name):
                self.assertTrue(_settlement_like(load(name)))

    def test_settlement_only_input_yields_no_evidence(self) -> None:
        frames = [load(name) for name in RESULT_FRAMES]

        self.assertEqual(select_evidence_frames(frames, need=2, include_last=True), [])

    def test_settlement_frame_is_skipped_but_snow_frame_kept(self) -> None:
        snow, result = load(SNOW_FRAMES[0]), load(RESULT_FRAMES[0])

        selected = select_evidence_frames([snow, result], need=1, include_last=True)

        self.assertEqual(selected, [snow])


if __name__ == "__main__":
    unittest.main()
