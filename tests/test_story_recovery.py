"""战后剧情推进与退出证据复检的离线契约测试。

战斗结束后可能停在战后剧情页：画面非战斗且没有结算证据，旧实现直接
EXIT_UNCONFIRMED；手动点右上「跳过」再点确认后进入结算或地图。

这里锁定四件事：
  - 剧情推进必须用画面探测证明已经离开剧情页，点击成功不算结束；
  - 没有剧情证据时一次都不点，更不去点暂停；
  - 剧情推进失败/探测无结论时绝不宣称退出；
  - runner 只在「非战斗 + 面板与结算都没有证据」时才尝试推进剧情。
"""
from __future__ import annotations

import unittest
from unittest import mock

from src.engine import navigator, runner


class ScriptedNative:
    """按脚本回放原生链结果，并记录每次调用的链名与 overlay。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def __call__(self, names, *, overlay=None, timeout=60, **kw):
        self.calls.append({"names": list(names), "overlay": overlay,
                           "timeout": timeout})
        if not self.script:
            raise AssertionError(f"未预期的原生链调用：{names}")
        step = self.script.pop(0)
        return {"ok": step.get("ok", True),
                "marks": list(step.get("marks", [])),
                "nodes": list(step.get("nodes", [])),
                "results": {},
                "events_path": step.get("events_path", "events.jsonl")}


class AdvancePostBattleStoryTest(unittest.TestCase):
    def setUp(self):
        self.taps = []
        self.points = []
        self.found = {"跳过", "确认"}

        def fake_tap_text(text, *, timeout=20, **kw):
            self.taps.append(text)
            if text not in self.found:
                raise LookupError(f"未命中文字：{text}")
            return {"ok": True, "rect": [0, 0, 1, 1], "text": text}

        def fake_tap_point(x, y, **kw):
            self.points.append((x, y))
            return {"ok": True, "point": [x, y]}

        for name, fake in (("tap_text", fake_tap_text), ("tap_point", fake_tap_point)):
            patcher = mock.patch.object(navigator, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)

    def script_native(self, script):
        native = ScriptedNative(script)
        patcher = mock.patch.object(navigator, "run_native_chain", native)
        patcher.start()
        self.addCleanup(patcher.stop)
        return native

    def assert_no_pause_click(self):
        """只允许点「跳过」「确认」文字；永不按坐标盲点（如暂停按钮）。"""
        self.assertEqual(self.points, [], "不应有坐标盲点")
        self.assertTrue(set(self.taps) <= {"跳过", "确认", "確認"},
                        f"只允许点跳过/确认文字：{self.taps}")

    def test_story_skip_and_confirm_reaches_result_page(self):
        native = self.script_native([
            {"marks": ["MarkNavScreenStory"]},
            {"nodes": ["StorySkipPost", "SkipBattlePlot", "SkipBattlePlotConfirm"]},
            {"marks": ["MarkNavScreenResult"]},
        ])
        result = navigator.advance_post_battle_story()

        self.assertTrue(result["advanced"])
        self.assertEqual(result["reason"], "advanced")
        self.assertEqual(result["before"], "story")
        self.assertEqual(result["after"], "result")
        self.assertTrue(result["clicked"])
        self.assertEqual(result["attempts"], 1)
        # 原生模板已经点到跳过+确认：不再需要 OCR 兜底。
        self.assertEqual(self.taps, [])
        self.assertEqual([call["names"] for call in native.calls],
                         [["NavScreenProbe"], ["StorySkipPost"], ["NavScreenProbe"]])
        self.assertEqual([call["overlay"] for call in native.calls],
                         [navigator.NAV_OVERLAY, navigator.STORY_SKIP_OVERLAY,
                          navigator.NAV_OVERLAY])
        self.assert_no_pause_click()

    def test_native_miss_falls_back_to_ocr_skip_then_confirm(self):
        self.script_native([
            {"marks": ["MarkNavScreenStory"]},
            {"marks": ["MarkStoryRetryLimit"],
             "nodes": ["StorySkipPost", "StoryWaitPost"]},
            {"marks": ["MarkNavScreenPanel"]},
        ])
        result = navigator.advance_post_battle_story()

        self.assertTrue(result["advanced"])
        self.assertEqual(result["after"], "panel")
        self.assertEqual(self.taps, ["跳过", "确认"])
        self.assertEqual(result["ocr"], ["跳过", "确认"])
        self.assert_no_pause_click()

    def test_no_story_screen_never_clicks(self):
        native = self.script_native([{"marks": ["MarkNavScreenResult"]}])
        result = navigator.advance_post_battle_story()

        self.assertFalse(result["advanced"])
        self.assertEqual(result["reason"], "no_story")
        self.assertEqual(result["attempts"], 0)
        self.assertEqual(len(native.calls), 1, "没有剧情页就不该跑跳过链")
        self.assertEqual(self.taps, [])
        self.assert_no_pause_click()

    def test_unknown_screen_never_clicks(self):
        native = self.script_native([{"marks": []}])
        result = navigator.advance_post_battle_story()

        self.assertFalse(result["advanced"])
        self.assertEqual(result["reason"], "screen_unknown")
        self.assertEqual(len(native.calls), 1)
        self.assertEqual(self.taps, [])
        self.assert_no_pause_click()

    def test_failed_skip_does_not_claim_plot_finished(self):
        self.found = set()
        self.script_native([
            {"marks": ["MarkNavScreenStory"]},
            {"marks": ["MarkStoryRetryLimit"],
             "nodes": ["StorySkipPost", "StoryWaitPost"]},
            {"marks": ["MarkNavScreenStory"]},
        ])
        result = navigator.advance_post_battle_story(max_attempts=1)

        self.assertFalse(result["advanced"])
        self.assertEqual(result["reason"], "still_story")
        self.assertEqual(result["after"], "story")
        self.assertFalse(result["clicked"])
        # OCR 只尝试点文字，未命中就绝不下手（简繁两种确认）。
        self.assertEqual(self.taps, ["跳过", "确认", "確認"])
        self.assertEqual(result["marks"].count("MarkNavScreenStory"), 2)
        self.assert_no_pause_click()

    def test_click_with_unknown_verification_is_not_advance(self):
        self.script_native([
            {"marks": ["MarkNavScreenStory"]},
            {"nodes": ["SkipBattlePlot", "SkipBattlePlotConfirm"]},
            {"marks": []},
        ])
        result = navigator.advance_post_battle_story()

        self.assertFalse(result["advanced"], "点击成功不能当剧情结束")
        self.assertEqual(result["reason"], "screen_unknown")
        self.assertTrue(result["clicked"])
        self.assertEqual(result["after"], "unknown")
        self.assert_no_pause_click()

    def test_end_of_plot_mark_alone_is_not_success(self):
        self.script_native([
            {"marks": ["MarkNavScreenStory"]},
            {"marks": ["EndOfPlot"],
             "nodes": ["SkipBattlePlot", "SkipBattlePlotConfirm"]},
            {"marks": ["MarkNavScreenStory"]},
        ])
        result = navigator.advance_post_battle_story(max_attempts=1)

        self.assertTrue(result["end_of_plot"])
        self.assertFalse(result["advanced"], "EndOfPlot 不能替代画面证据")
        self.assertEqual(result["reason"], "still_story")
        self.assert_no_pause_click()

    def test_story_persisting_retries_then_reports_story(self):
        native = self.script_native([
            {"marks": ["MarkNavScreenStory"]},
            {"nodes": ["SkipBattlePlot"]},
            {"marks": ["MarkNavScreenStory"]},
            {"nodes": ["SkipBattlePlot", "SkipBattlePlotConfirm"]},
            {"marks": ["MarkNavScreenResult"]},
        ])
        result = navigator.advance_post_battle_story(max_attempts=2)

        self.assertTrue(result["advanced"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(len(native.calls), 5)
        self.assert_no_pause_click()


class ResolveExitEvidenceTest(unittest.TestCase):
    def run_evidence(self, scene, *, panels, results, story=None):
        panel_calls = []
        result_calls = []
        story_calls = []

        def panel_probe():
            panel_calls.append(1)
            return panels.pop(0)

        def result_probe():
            result_calls.append(1)
            return results.pop(0)

        def story_probe():
            story_calls.append(1)
            return story if story is not None else {"advanced": False,
                                                    "reason": "no_story"}

        evidence = runner.resolve_exit_evidence(
            scene, panel_probe=panel_probe, result_probe=result_probe,
            story_probe=story_probe)
        return evidence, panel_calls, result_calls, story_calls

    def test_story_advanced_reprobes_panel(self):
        evidence, panel_calls, result_calls, story_calls = self.run_evidence(
            "not_battle", panels=[False, True], results=[None],
            story={"advanced": True, "reason": "advanced", "after": "panel"})

        self.assertTrue(evidence["panel"])
        self.assertIsNone(evidence["result_page"])
        self.assertEqual(len(story_calls), 1)
        self.assertEqual(len(panel_calls), 2)
        self.assertEqual(len(result_calls), 1)
        self.assertEqual(runner.exit_decision(
            "not_battle", panel_confirmed=evidence["panel"],
            result_page=evidence["result_page"]), "panel")

    def test_story_advanced_reprobes_result_page(self):
        evidence, _, result_calls, _ = self.run_evidence(
            "not_battle", panels=[False, False], results=[None, "won"],
            story={"advanced": True, "reason": "advanced", "after": "result"})

        self.assertFalse(evidence["panel"])
        self.assertEqual(evidence["result_page"], "won")
        self.assertEqual(len(result_calls), 2)
        self.assertEqual(runner.exit_decision(
            "not_battle", panel_confirmed=evidence["panel"],
            result_page=evidence["result_page"]), "result_page")

    def test_failed_story_advance_keeps_unknown(self):
        evidence, panel_calls, result_calls, _ = self.run_evidence(
            "not_battle", panels=[False], results=[None],
            story={"advanced": False, "reason": "still_story", "after": "story"})

        self.assertFalse(evidence["panel"])
        self.assertIsNone(evidence["result_page"])
        self.assertFalse(evidence["story"]["advanced"])
        self.assertEqual(len(panel_calls), 1, "没有画面证据就不该复检面板")
        self.assertEqual(len(result_calls), 1)
        self.assertEqual(runner.exit_decision(
            "not_battle", panel_confirmed=evidence["panel"],
            result_page=evidence["result_page"]), "unknown")

    def test_panel_confirmed_skips_story_click(self):
        evidence, _, result_calls, story_calls = self.run_evidence(
            "not_battle", panels=[True], results=[])
        self.assertTrue(evidence["panel"])
        self.assertIsNone(evidence["story"])
        self.assertEqual(story_calls, [], "已在详情面板时不该推进剧情")
        self.assertEqual(result_calls, [])

    def test_result_page_found_skips_story_click(self):
        evidence, _, _, story_calls = self.run_evidence(
            "not_battle", panels=[False], results=["failed"])
        self.assertEqual(evidence["result_page"], "failed")
        self.assertEqual(story_calls, [], "已有结算证据时不该推进剧情")

    def test_in_battle_or_paused_never_advances_story(self):
        for scene in ("live", "paused", "unknown"):
            evidence, panel_calls, result_calls, story_calls = self.run_evidence(
                scene, panels=[], results=[])
            self.assertEqual(story_calls, [], f"{scene} 不得点剧情/暂停")
            self.assertEqual(panel_calls, [])
            self.assertEqual(result_calls, [])
            self.assertFalse(evidence["panel"])
            self.assertIsNone(evidence["result_page"])
            self.assertIsNone(evidence["story"])


class StoryExitConfirmedTest(unittest.TestCase):
    """剧情页离开 = 战斗结束的画面证据；其余画面一律不成立。"""

    def test_advanced_story_without_result_page_confirms_exit(self):
        self.assertTrue(runner.story_exit_confirmed(
            "not_battle", panel_confirmed=False, result_page=None,
            story={"advanced": True, "reason": "advanced", "after": "unknown"}))

    def test_unadvanced_story_keeps_exit_unconfirmed(self):
        self.assertFalse(runner.story_exit_confirmed(
            "not_battle", panel_confirmed=False, result_page=None,
            story={"advanced": False, "reason": "still_story", "after": "story"}))

    def test_missing_story_keeps_exit_unconfirmed(self):
        self.assertFalse(runner.story_exit_confirmed(
            "not_battle", panel_confirmed=False, result_page=None, story=None))

    def test_in_battle_never_confirms_exit(self):
        for scene in ("live", "paused", "unknown"):
            self.assertFalse(runner.story_exit_confirmed(
                scene, panel_confirmed=False, result_page=None,
                story={"advanced": True, "reason": "advanced", "after": "result"}))

    def test_panel_or_result_page_keeps_its_own_verdict(self):
        self.assertFalse(runner.story_exit_confirmed(
            "not_battle", panel_confirmed=True, result_page=None,
            story={"advanced": True, "reason": "advanced", "after": "panel"}))
        self.assertFalse(runner.story_exit_confirmed(
            "not_battle", panel_confirmed=False, result_page="won",
            story={"advanced": True, "reason": "advanced", "after": "result"}))


if __name__ == "__main__":
    unittest.main()
