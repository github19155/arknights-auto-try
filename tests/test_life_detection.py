"""生命点扣血检测与任务失败页退出归位的回归测试。

覆盖两条关键故障路径：
  - HUD 数字渐入被锁成基线，导致整局扣血检测静默失效；数字变亮不是掉血。
  - 退出决策把任务失败页当活战斗，放弃链盲点暂停后失败。

这些用例只依赖 tests/assets 里的 640×360 合成素材与仓库内 MAA 任务链定义，
不连模拟器，也不读运行时录像目录。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from PIL import Image, ImageEnhance

import numpy as np

from src.engine.recorder import (
    DIGIT_TIGHT_ROI,
    hp_digit_ready,
    hp_digit_shape,
    hp_digit_shape_changed,
    is_battle_hud,
)
from src.engine.runner import (
    LeakSampler,
    evidence_frame_meta,
    evidence_with_pin,
    exit_decision,
    leak_evidence_payload,
)

ASSETS = Path(__file__).resolve().parent / "assets"
TASKS = (Path(__file__).resolve().parents[1]
         / "tools" / "maa-resources" / "abandon-battle" / "resource" / "tasks.json")


def asset(name: str) -> Image.Image:
    return Image.open(ASSETS / f"{name}.png").convert("RGB")


def faded_hud_frame(source: Image.Image, strength: float = 0.85) -> Image.Image:
    """模拟 HUD 渐入帧：生命数字还没画完，但生命点菱形已经可见。

    旧实现会把这种帧锁成基线，之后数字继续变亮就被判成字形变化，整局检测
    静默失效。strength 越大，数字与背景越接近。
    """
    frame = source.copy()
    sx, sy = frame.width / 1280, frame.height / 720
    box = (round(DIGIT_TIGHT_ROI[0] * sx), round(DIGIT_TIGHT_ROI[1] * sy),
           round((DIGIT_TIGHT_ROI[0] + DIGIT_TIGHT_ROI[2]) * sx),
           round((DIGIT_TIGHT_ROI[1] + DIGIT_TIGHT_ROI[3]) * sy))
    patch = frame.crop(box)
    background = Image.new("RGB", patch.size, patch.getpixel((0, 0)))
    frame.paste(Image.blend(patch, background, strength), (box[0], box[1]))
    return frame


#: HUD 渐入的近似节奏：数字从暗到亮，命中数一路爬升到全亮。
FADE_STRENGTHS = tuple(round(0.40 - 0.04 * index, 2) for index in range(11))


def brightness_normalized_iou(first: Image.Image, second: Image.Image) -> float:
    """把两帧紧致数字窗口各自按最亮像素归一后比较掩膜 IoU。

    同一字形从暗变亮时，原始绝对阈值掩膜会因命中数变化而“变形”；亮度归一化
    后仍高度重叠。这个纯测试辅助用来证明两帧其实是同一字形。
    """
    def mask(frame):
        x, y, w, h = DIGIT_TIGHT_ROI
        sx, sy = frame.width / 1280, frame.height / 720
        patch = frame.crop((round(x * sx), round(y * sy),
                            round((x + w) * sx), round((y + h) * sy))).convert("RGB")
        arr = np.asarray(patch, dtype=np.float32)
        arr = arr * (255.0 / max(float(arr.max()), 1.0))
        r, g, b = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2]
        return (r > 150) & (g < 150) & (r > b + 25)

    a, b = mask(first), mask(second)
    union = int((a | b).sum())
    return float((a & b).sum()) / union if union else 0.0


class Feed:
    """把同一张画面按采样节拍喂给扣血状态机，返回确认到的扣血事件。"""

    def __init__(self) -> None:
        self.sampler = LeakSampler()
        self.index = -1

    def feed(self, *frames: Image.Image) -> list[dict]:
        leaks = []
        for frame in frames:
            self.index += 1
            fact = self.sampler.observe(frame, frame_index=self.index)
            if fact is not None:
                leaks.append(fact)
        return leaks


class LeakSamplerTest(unittest.TestCase):
    def test_life3_to_life2_needs_two_consecutive_frames(self):
        """素材 3→2：单帧不算，连续两帧确认，且记住首扣血帧。"""
        feed = Feed()
        self.assertEqual(feed.feed(asset("life-3"), asset("life-3")), [])
        self.assertIsNotNone(feed.sampler.baseline_shape, "两帧稳定后必须锁定基线")

        self.assertEqual(feed.feed(asset("life-2")), [], "单帧变化不能当确认")
        leaks = feed.feed(asset("life-2"))

        self.assertEqual(len(leaks), 1)
        leak = leaks[0]
        self.assertTrue(leak["digit_shape_changed"])
        self.assertEqual(leak["first_changed_frame_index"], 2)
        self.assertEqual(leak["confirmed_at_frame_index"], 3)
        self.assertGreaterEqual(leak["digit_baseline_hits"], 24)
        self.assertGreater(leak["digit_baseline_hits"], leak["digit_current_hits"])

    def test_life2_to_life1_is_detected(self):
        """素材 2→1：基线是「2」时也必须检测（旧实现 base_n 太小直接放弃）。"""
        feed = Feed()
        self.assertEqual(feed.feed(asset("life-2"), asset("life-2")), [])
        self.assertEqual(feed.feed(asset("life-1")), [])
        leaks = feed.feed(asset("life-1"))
        self.assertEqual(len(leaks), 1)
        self.assertEqual(leaks[0]["confirmed_at_frame_index"], 3)

    def test_single_recovered_frame_is_not_a_leak(self):
        """一帧像掉血、下一帧又回来（动画/特效）不能确认漏怪。"""
        feed = Feed()
        feed.feed(asset("life-3"), asset("life-3"))
        self.assertEqual(feed.feed(asset("life-2")), [])
        self.assertEqual(feed.feed(asset("life-3")), [])
        self.assertEqual(feed.feed(asset("life-2")), [])

    def test_fade_in_baseline_does_not_disable_detection(self):
        """回归用例：HUD 渐入帧不得被当成基线，否则整局静默漏报。"""
        fade = faded_hud_frame(asset("life-3"))
        self.assertTrue(is_battle_hud(fade), "渐入帧的生命点菱形已可见")
        self.assertFalse(hp_digit_ready(hp_digit_shape(fade)),
                         "渐入帧的数字字形还没画完")

        feed = Feed()
        self.assertEqual(feed.feed(fade), [])
        self.assertIsNone(feed.sampler.baseline_shape, "未画完的帧不能锁基线")
        self.assertEqual(feed.feed(asset("life-3"), asset("life-3")), [])
        self.assertIsNotNone(feed.sampler.baseline_shape)

        feed.feed(asset("life-2"))
        self.assertEqual(len(feed.feed(asset("life-2"))), 1)

    def test_progressive_fade_in_never_triggers_leak(self):
        """回归用例：数字从暗到亮的整个渐入序列不得报掉血。

        合成复现场景：战场刚出现、生命仍是 3/3 时 LeakDetected 触发，
        画面是 HUD 数字在渐入。旧实现会在命中数约 40 的渐入中段锁基线
        （相邻帧命中差只有几个像素），之后继续变亮被当字形变化，连续两帧
        确认成漏怪。这里的强度序列在旧实现下会复现该假阳性。
        """
        feed = Feed()
        frames = [faded_hud_frame(asset("life-3"), strength)
                  for strength in FADE_STRENGTHS]
        self.assertTrue(all(is_battle_hud(frame) for frame in frames),
                        "渐入期生命点菱形已经可见，旧基线条件会成立")
        self.assertEqual(feed.feed(*frames), [], "数字从暗变亮不是掉血")
        self.assertIsNone(feed.sampler.baseline_shape,
                          "命中数仍在爬升/亮度未收敛时不能锁基线")

    def test_leak_after_fade_in_is_detected(self):
        """渐入结束、基线收敛后，素材 3→2 仍必须触发。"""
        feed = Feed()
        life3 = asset("life-3")
        frames = [faded_hud_frame(life3, strength) for strength in FADE_STRENGTHS]
        self.assertEqual(feed.feed(*frames), [])
        self.assertEqual(feed.feed(life3, life3), [])
        self.assertIsNotNone(feed.sampler.baseline_shape, "亮度收敛后才能锁基线")

        self.assertEqual(feed.feed(asset("life-2")), [], "单帧变化不能当确认")
        leaks = feed.feed(asset("life-2"))
        self.assertEqual(len(leaks), 1, "渐入后的扣血必须能识别")
        self.assertLess(leaks[0]["digit_current_hits"],
                        leaks[0]["digit_baseline_hits"])

    def test_hit_growth_on_same_glyph_is_not_a_shape_change(self):
        """命中数增加 + 亮度归一化 IoU 高（同一「3」）不触发。"""
        dim = faded_hud_frame(asset("life-3"), 0.28)
        bright = faded_hud_frame(asset("life-3"), 0.04)
        base, current = hp_digit_shape(dim), hp_digit_shape(bright)
        self.assertGreater(current[1], base[1] * 1.5, "渐入后命中数显著增加")

        diff = sum(1 for a, b in zip(base[0], current[0]) if a != b)
        overlap = sum(1 for a, b in zip(base[0], current[0]) if a and b)
        union = base[1] + current[1] - overlap
        self.assertGreater(diff / union, 0.4, "原始掩膜判据会把它当字形变化")
        self.assertGreater(brightness_normalized_iou(dim, bright), 0.9,
                           "亮度归一化后仍是同一「3」字形")
        self.assertFalse(hp_digit_shape_changed(base, current),
                         "命中数增加是渐入，不是掉血")

    def test_life_loss_still_has_low_glyph_overlap(self):
        """扣血前后的字形重叠明显下降，必须触发。"""
        for before, after in (("life-3", "life-2"),
                              ("life-2", "life-1")):
            base = hp_digit_shape(asset(before))
            current = hp_digit_shape(asset(after))
            overlap = sum(1 for a, b in zip(base[0], current[0]) if a and b)
            union = base[1] + current[1] - overlap
            self.assertLess(overlap / union, 0.6,
                            f"{before}→{after} 的字形重叠应明显下降")
            self.assertTrue(hp_digit_shape_changed(base, current),
                            f"{before}→{after} 必须识别为掉血")

    def test_sampler_reports_real_sample_times(self):
        """传感事实带出基线/首变/确认的单调时钟与相对秒，而不是帧序号。"""
        stamps = [
            {"sampled_at": 100.0, "battle_s": 1.0, "video_s": 3.0},
            {"sampled_at": 100.15, "battle_s": 1.15, "video_s": 3.15},
            {"sampled_at": 100.3, "battle_s": 1.3, "video_s": 3.3},
            {"sampled_at": 100.45, "battle_s": 1.45, "video_s": 3.45},
        ]
        frames = []
        for name, stamp in zip(("life-3", "life-3", "life-2", "life-2"),
                               stamps):
            frame = asset(name)
            frame.info.update(stamp)
            frames.append(frame)

        feed = Feed()
        self.assertEqual(feed.feed(*frames[:3]), [])
        leaks = feed.feed(frames[3])
        self.assertEqual(len(leaks), 1)
        leak = leaks[0]
        self.assertEqual(leak["baseline_sampled_at"], 100.0)
        self.assertEqual(leak["first_changed_sampled_at"], 100.3)
        self.assertEqual(leak["confirmed_sampled_at"], 100.45)
        self.assertEqual(leak["confirmed_battle_s"], 1.45)
        self.assertEqual(leak["confirmed_video_s"], 3.45)

    def test_dimmed_battle_is_not_a_leak(self):
        """非漏怪的整屏压暗（选中干员/部署面板）不得报漏怪。"""
        feed = Feed()
        feed.feed(asset("life-3"), asset("life-3"))
        life3 = asset("life-3")
        for factor in (0.9, 0.8, 0.7, 0.55, 0.35):
            dim = ImageEnhance.Brightness(life3).enhance(factor)
            self.assertEqual(feed.feed(dim, dim, dim), [], f"压暗 {factor} 误报漏怪")
        self.assertEqual(feed.feed(asset("battle-operator-panel")), [])

    def test_digit_shape_is_resolution_invariant(self):
        """同一画面在 640×360 / 1280×720 下的字形命中数必须同量级。

        运行帧常是 1280×720，而 fixture 是 640×360；旧实现按绝对像素数比较，
        2→1 在低分辨率下永远判不出变化。
        """
        low = hp_digit_shape(asset("life-3"))
        high = hp_digit_shape(asset("life-3").resize((1280, 720), Image.Resampling.NEAREST))
        self.assertTrue(hp_digit_ready(low) and hp_digit_ready(high))
        self.assertLess(abs(low[1] - high[1]) / max(low[1], high[1]), 0.15)


class LeakEvidenceTest(unittest.TestCase):
    def test_first_leak_frame_replaces_oldest_tile(self):
        """证据四格要保留首扣血画面，同时保留冻结前最近有效帧。"""
        ring = [Image.new("RGB", (8, 8), (i * 10, 0, 0)) for i in range(4)]
        pin = Image.new("RGB", (8, 8), (99, 0, 0))
        picked = evidence_with_pin(ring, pin)
        self.assertEqual(len(picked), 4)
        self.assertIs(picked[0], pin)
        self.assertIs(picked[-1], ring[-1], "冻结前最近有效帧不能被挤掉")
        self.assertEqual(picked[1:], ring[1:])

    def test_frame_already_in_grid_is_not_duplicated(self):
        ring = [Image.new("RGB", (8, 8), (i * 10, 0, 0)) for i in range(4)]
        self.assertEqual(evidence_with_pin(ring, ring[1]), ring)
        self.assertEqual(evidence_with_pin(ring, None), ring)

    def test_grid_metadata_traces_real_sample_times(self):
        """四宫格清单必须写每格的采样时间；排序/复核不能靠帧序号。"""
        ring = [Image.new("RGB", (8, 8)) for _ in range(4)]
        for index, frame in enumerate(ring):
            frame.info.update({
                "frame_index": index * 10,
                "sampled_at": 200.0 + index,
                "battle_s": 5.0 + index,
                "video_s": 9.0 + index,
            })
        pin = Image.new("RGB", (8, 8))
        pin.info.update({"frame_index": 99, "sampled_at": 199.5,
                         "battle_s": 4.5, "video_s": 8.5})

        meta = evidence_frame_meta(ring, pin=pin)
        self.assertEqual([item["sampled_at"] for item in meta],
                         [200.0, 201.0, 202.0, 203.0])
        self.assertEqual([item["source"] for item in meta], ["ring"] * 4)

        picked = evidence_frame_meta([pin] + ring[1:], pin=pin)
        self.assertEqual(picked[0]["source"], "first_change_pin")
        self.assertEqual(picked[0]["sampled_at"], 199.5)
        self.assertEqual(picked[0]["battle_s"], 4.5)

    def test_missing_sample_time_stays_null(self):
        """没有采样时间的帧不得拿帧序号冒充时间。"""
        frame = Image.new("RGB", (8, 8))
        frame.info["frame_index"] = 7
        meta = evidence_frame_meta([frame])
        self.assertIsNone(meta[0]["sampled_at"])
        self.assertIsNone(meta[0]["battle_s"])
        self.assertIsNone(meta[0]["video_s"])


class LeakEvidencePayloadTest(unittest.TestCase):
    """sidecar 载荷是纯数据：不读帧、不碰设备，能直接落盘。"""

    def test_payload_carries_png_stage_leak_frames_and_time_basis(self):
        frames = [{"slot": 1, "source": "first_change_pin",
                   "sampled_at": 1000.0, "battle_s": 10.0,
                   "video_s": 12.0}]
        leak = {"confirmed_sampled_at": 1010.0,
                "confirmed_battle_s": 20.0,
                "hp_before": [230, 92], "hp_after": [21, 67]}
        payload = leak_evidence_payload(
            png=Path("captures") / "XX-8" / "leak-evidence-synthetic-00000000-000000-000.png",
            stage_code="XX-8", leak=leak, frames=frames)

        # sidecar 与 PNG 同目录，只记文件名，搬移录屏目录后仍可解析。
        self.assertEqual(payload["png"], "leak-evidence-synthetic-00000000-000000-000.png")
        self.assertEqual(payload["stage_code"], "XX-8")
        self.assertEqual(payload["leak"], leak)
        self.assertEqual(payload["frames"], frames)
        self.assertIn("leak_battle_s", payload["time_basis"])
        self.assertEqual(json.loads(json.dumps(payload)), payload)


class ExitDecisionTest(unittest.TestCase):
    def test_failure_page_is_identified_not_abandoned(self):
        """任务失败页：原生 OCR 命中就走归位，绝不盲点 BattlePause。"""
        self.assertEqual(
            exit_decision("not_battle", panel_confirmed=False, result_page="failed"),
            "result_page")
        self.assertEqual(
            exit_decision("not_battle", panel_confirmed=False, result_page="won"),
            "result_page")

    def test_control_block_gone_alone_is_not_a_result_page(self):
        """右上控制块消失 + 没有原生 OCR/模板证据 = unknown，不能推断结算。"""
        self.assertEqual(
            exit_decision("not_battle", panel_confirmed=False, result_page=None),
            "unknown")

    def test_live_battle_still_goes_through_abandon(self):
        self.assertEqual(exit_decision("live", panel_confirmed=False, result_page=None),
                         "abandon")
        self.assertEqual(exit_decision("paused", panel_confirmed=False, result_page=None),
                         "abandon")

    def test_panel_confirmation_wins(self):
        self.assertEqual(exit_decision("not_battle", panel_confirmed=True, result_page=None),
                         "panel")

    def test_unknown_never_claims_exit(self):
        """抓帧失败/模板缺失：任何输入都不能得到「已退出」的裁决。"""
        for scene in ("unknown", "not_battle"):
            verdict = exit_decision(scene, panel_confirmed=False, result_page=None)
            self.assertEqual(verdict, "unknown", f"{scene} 不得当作已退出")

    def test_unknown_is_conservative_even_with_stale_marks(self):
        """画面未知时，即使别的链留下过标记也不能宣称退出。"""
        self.assertEqual(exit_decision("unknown", panel_confirmed=False, result_page=None),
                         "unknown")


class AbandonChainFailurePageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tasks = json.loads(TASKS.read_text(encoding="utf-8"))

    def test_failure_page_guard_precedes_blind_pause(self):
        """放弃链自身也必须先看失败页证据，再考虑盲点暂停。"""
        entry = self.tasks["AbandonEntry"]
        self.assertIn("BattlePause", entry["next"], "没有暂停证据时仍需保底重试")
        guard_name = "AbandonResultGuard"
        self.assertIn(guard_name, entry["next"])
        self.assertLess(entry["next"].index(guard_name), entry["next"].index("BattlePause"))

        guard = self.tasks[guard_name]
        self.assertEqual(guard["action"], "DoNothing", "识别节点不能点任何按钮")
        self.assertEqual(guard["baseTask"], "FightMissionFailed", "必须复用原生失败 OCR")
        self.assertIn("BattlePause", guard.get("exceededNext", []),
                      "未命中时要回到原来的盲点暂停保底路径")
        self.assertEqual(guard["next"], ["MarkAbandonResultPage"])

        mark = self.tasks["MarkAbandonResultPage"]
        self.assertEqual(mark["action"], "Stop")
        self.assertNotIn("MarkBattleAbandoned", json.dumps(self.tasks[guard_name]),
                         "失败页识别不能伪造原生放弃标记")

    def test_result_page_mark_does_not_impersonate_abandon(self):
        sources = [name for name, node in self.tasks.items()
                   if "MarkBattleAbandoned" in (node.get("next") or [])]
        self.assertNotIn("MarkAbandonResultPage", sources)


if __name__ == "__main__":
    unittest.main()
