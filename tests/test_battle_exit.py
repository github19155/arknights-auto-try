"""战斗画面分类与原生放弃链的离线契约测试。

暂停控件由测试现场绘制，不使用实机画面，也不连接模拟器。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageEnhance
import numpy as np

from battle_control_fixtures import battle_control, write_battle_control_templates

from src.engine.recorder import (
    BATTLE_CONTROL_ROI,
    BATTLE_PAUSE_BUTTON,
    BATTLE_PAUSE_MENU,
    battle_scene_state,
    is_battle_hud,
    parse_maa_events,
    result_three_star,
)
from src.engine.runner import (
    RESULT_PAGE_VERDICTS,
    abandon_result_verdict,
    battle_evidence,
    battle_exit_verdict,
    battle_result_verdict,
    cleanup_warning_event,
    ended_exit_allowed,
    exit_decision,
    failure_reason,
    select_battle_result,
    select_run_status,
    verified_native_battle,
    won_evidence_verdict,
)

ASSETS = Path(__file__).resolve().parent / "assets"
TASKS = (Path(__file__).resolve().parents[1]
         / "tools" / "maa-resources" / "abandon-battle" / "resource" / "tasks.json")
RESULT_TASKS = (Path(__file__).resolve().parents[1]
                / "tools" / "maa-resources" / "result-probe" / "resource" / "tasks.json")
# 通用结算页标题：不同版本分别是「行动结束」与「行动结果」。
GENERIC_SETTLEMENT_TITLES = ("行动结束", "行動結束", "行动结果", "行動結果",
                             "MISSION RESULTS", "MISSIONRESULTS")


def battle_frame(state: str) -> Image.Image:
    """在 1280×720 合成画布上放置对应的右上控件。"""
    canvas = Image.new("RGB", (1280, 720), (6, 8, 10))
    if state in {"live", "paused"}:
        canvas.paste(battle_control(paused=state == "paused"), (1185, 36))
    return canvas


def load_tasks() -> dict:
    return json.loads(TASKS.read_text(encoding="utf-8"))


def load_result_tasks() -> dict:
    return json.loads(RESULT_TASKS.read_text(encoding="utf-8"))


def edges(tasks: dict) -> dict[str, set[str]]:
    """节点图：next / exceededNext / onErrorNext（不含 baseTask 继承）。"""
    graph: dict[str, set[str]] = {}
    for name, node in tasks.items():
        targets: set[str] = set()
        for key in ("next", "exceededNext", "onErrorNext"):
            values = node.get(key) or []
            if isinstance(values, str):
                values = [values]
            targets.update(values)
        graph[name] = {t for t in targets if t in tasks}
    return graph


def paths_to(graph: dict[str, set[str]], start: str, goal: str) -> list[list[str]]:
    found: list[list[str]] = []

    def walk(node: str, path: list[str]) -> None:
        if node == goal:
            found.append(path + [node])
            return
        for nxt in sorted(graph.get(node, ())):
            if nxt in path:
                continue  # 自引用重试不算新路径
            walk(nxt, path + [node])

    walk(start, [])
    return found


class BattleSceneStateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.live = Path(tmp.name) / "live.png"
        self.paused = Path(tmp.name) / "paused.png"
        write_battle_control_templates(self.live, self.paused)

    def classify(self, frame):
        return battle_scene_state(frame, live_template=self.live, paused_template=self.paused)

    def test_live_battle_without_life_dots_is_not_exit(self):
        """无生命点不等于退出：右上暂停按钮仍在就是活战斗。"""
        frame = battle_frame("live")
        self.assertFalse(is_battle_hud(frame), "测试帧刻意没有生命点")
        self.assertEqual(self.classify(frame), "live")
        # 即使原生链已经写过退出标记（不该发生），活战斗画面也不能算退出。
        self.assertEqual(battle_exit_verdict("live", {"MarkBattleAbandoned"}), "in_battle")

    def test_paused_menu_is_not_exit(self):
        """暂停菜单：生命点可能被遮罩压暗，画面态必须是 paused 而不是退出。"""
        self.assertEqual(self.classify(battle_frame("paused")), "paused")
        self.assertEqual(
            battle_exit_verdict("paused", {"MarkBattleAbandoned", "MarkAtPanel"}),
            "in_battle")

    def test_dimmed_battle_is_still_battle(self):
        """选中干员/暂停遮罩会整体压暗：临时压暗不能把活战斗判成已退出。"""
        for expected in ("live", "paused"):
            frame = battle_frame(expected)
            for factor in (0.8, 0.6, 0.45):
                dimmed = ImageEnhance.Brightness(frame).enhance(factor)
                self.assertEqual(self.classify(dimmed), expected)

    def test_no_control_block_is_unknown_not_exit(self):
        """看不到控制块只说明画面不确定，没有原生证据时一律 unknown。"""
        self.assertEqual(self.classify(battle_frame("panel")), "not_battle")
        self.assertEqual(battle_exit_verdict("not_battle", set()), "unknown")
        self.assertEqual(battle_exit_verdict("not_battle", {"MarkAbandonFailed"}), "unknown")

    def test_exit_requires_native_mark(self):
        """只有非战斗画面 + 原生 MarkBattleAbandoned 才是退出。"""
        self.assertEqual(
            battle_exit_verdict("not_battle", {"MarkBattleAbandoned"}), "exited")

    def test_unknown_never_exits(self):
        self.assertEqual(battle_exit_verdict("unknown", {"MarkBattleAbandoned"}), "unknown")
        self.assertEqual(self.classify(None), "unknown")
        self.live.unlink()
        self.assertEqual(self.classify(battle_frame("live")), "unknown")

    def test_scaled_frame_uses_same_roi(self):
        """不同分辨率按 1280×720 基准缩放 ROI，暂停态仍可识别。"""
        frame = battle_frame("paused").resize((960, 540))
        self.assertEqual(self.classify(frame), "paused")

    def test_control_roi_covers_pause_button(self):
        x, y, width, height = BATTLE_CONTROL_ROI
        self.assertLessEqual(x, 1170)
        self.assertLessEqual(y, 20)
        self.assertGreaterEqual(x + width, 1230)
        self.assertGreaterEqual(y + height, 80)


class BattleEvidenceTest(unittest.TestCase):
    def test_timeout_with_actions_requires_abandon(self):
        """超时且已执行过动作：即使画面探针说 not_battle 也必须走放弃。"""
        self.assertTrue(battle_evidence("not_battle", actions=11, leaked=False))
        self.assertTrue(battle_evidence("unknown", actions=1, leaked=False))

    def test_live_or_paused_frame_proves_battle(self):
        self.assertTrue(battle_evidence("live", actions=0, leaked=False))
        self.assertTrue(battle_evidence("paused", actions=0, leaked=False))

    def test_no_evidence_means_no_battle(self):
        self.assertFalse(battle_evidence("not_battle", actions=0, leaked=False))
        self.assertFalse(battle_evidence("unknown", actions=0, leaked=False))

    def test_leak_proves_battle(self):
        self.assertTrue(battle_evidence("not_battle", actions=0, leaked=True))


class AbandonChainTest(unittest.TestCase):
    def setUp(self):
        self.tasks = load_tasks()
        self.graph = edges(self.tasks)

    def node(self, name: str) -> dict:
        self.assertIn(name, self.tasks, f"缺少放弃链节点 {name}")
        return self.tasks[name]

    def test_entry_confirms_pause_before_menu(self):
        entry = self.node("AbandonEntry")
        self.assertEqual(entry["action"], "DoNothing",
                         "入口不能直接点放弃按钮，必须先拿暂停证据")
        self.assertIn("AbandonPausedMenu", entry["next"])
        self.assertIn("BattlePause", entry["next"])
        self.assertNotIn("AbandonConfirm", entry["next"])

        paused = self.node("AbandonPausedMenu")
        self.assertEqual(paused["algorithm"], "MatchTemplate")
        self.assertEqual(paused["template"], BATTLE_PAUSE_MENU)
        self.assertEqual(paused["action"], "DoNothing")
        self.assertEqual(paused["next"], ["AbandonOpenMenu"])

        live = self.node("AbandonPauseButton")
        self.assertEqual(live["template"], "Battle/BattleFlag/BattleOfficiallyBegin.png",
                         "活战斗应复用原生暂停模板")
        self.assertEqual(live["next"], ["BattlePause"])

    def test_pause_click_is_verified_by_pause_menu(self):
        pause = self.node("BattlePause")
        self.assertEqual(pause["action"], "ClickRect")
        self.assertEqual(pause["specificRect"], [1170, 20, 60, 60])
        self.assertIn("AbandonPausedMenu", pause["next"],
                      "点完暂停必须由暂停菜单模板确认")
        self.assertEqual(pause["exceededNext"], ["MarkAbandonFailed"])

    def test_menu_click_targets_top_left_settings(self):
        menu = self.node("AbandonOpenMenu")
        self.assertEqual(menu["action"], "ClickRect")
        x, y, width, height = menu["specificRect"]
        self.assertLessEqual(x, 80)
        self.assertLessEqual(y, 40)
        self.assertGreaterEqual(x + width, 90)
        self.assertGreaterEqual(y + height, 70)
        self.assertEqual(menu["next"], ["AbandonConfirm"])

    def test_confirm_uses_native_abandon_template(self):
        confirm = self.node("AbandonConfirm")
        self.assertEqual(confirm["algorithm"], "MatchTemplate")
        self.assertEqual(confirm["template"], "Roguelike/NormalBattleAbandon.png")
        self.assertEqual(confirm["action"], "ClickSelf")
        self.assertEqual(confirm["next"], ["AbandonExitProbe"])
        self.assertEqual(confirm["exceededNext"], ["MarkAbandonFailed"])
        x, y, width, height = confirm["roi"]
        self.assertGreaterEqual(x, 600, "确认按钮在原生放弃确认面板右下")
        self.assertGreaterEqual(y, 450)

    def test_native_mark_only_after_exit_evidence(self):
        mark = self.node("MarkBattleAbandoned")
        self.assertEqual(mark["action"], "Stop")
        sources = [name for name, targets in self.graph.items()
                   if "MarkBattleAbandoned" in targets]
        self.assertEqual(sorted(sources),
                         ["AbandonExitFailed", "AbandonExitPanel", "AbandonExitResult"])
        # 探测节点必须允许失败收尾，且不是无条件直达 MarkBattleAbandoned。
        probe = self.node("AbandonExitProbe")
        self.assertEqual(probe["exceededNext"], ["MarkAbandonFailed"])
        self.assertIn("AbandonExitProbe", probe["next"], "探测需要多次重试")

    def test_every_abandon_path_passes_pause_menu_confirm(self):
        found = paths_to(self.graph, "AbandonEntry", "MarkBattleAbandoned")
        self.assertTrue(found, "放弃链必须能到达 MarkBattleAbandoned")
        # 暂停证据（AbandonPausedMenu）→ 左上设置 → 原生确认，三步缺一不可；
        # 已暂停时允许跳过 BattlePause 这个点击节点，但证据节点不能跳过。
        required = {"AbandonPausedMenu", "AbandonOpenMenu", "AbandonConfirm"}
        for path in found:
            self.assertTrue(required.issubset(set(path)), f"绕过安全步骤的路径：{path}")


class AbandonResultVerdictTest(unittest.TestCase):
    """放弃链 OCR 闸门的纯函数裁决：失败页 / 结算页 / 没有证据三种。"""

    def test_no_guard_mark_means_no_verdict(self):
        self.assertIsNone(abandon_result_verdict(set()))
        # 原生放弃标记（另一条路径）不构成结算页证据。
        self.assertIsNone(abandon_result_verdict({"MarkBattleAbandoned"}))

    def test_failure_guard_maps_to_result_page(self):
        self.assertEqual(abandon_result_verdict({"MarkAbandonResultPage"}), "result_page")

    def test_action_ended_is_ended_not_won(self):
        """「行动结束」只说明战斗结束，不是胜利证据。"""
        self.assertEqual(abandon_result_verdict({"MarkAbandonActionEnded"}), "ended")
        self.assertNotIn(abandon_result_verdict({"MarkAbandonActionEnded"}), {"won", "failed"})

    def test_failure_guard_wins_over_action_ended(self):
        marks = {"MarkAbandonResultPage", "MarkAbandonActionEnded"}
        self.assertEqual(abandon_result_verdict(marks), "result_page")


class EndedExitAllowedTest(unittest.TestCase):
    """结算证据之后的画面复核：只有离开战斗才允许确认退出。"""

    def test_not_battle_allows_exit(self):
        self.assertTrue(ended_exit_allowed("not_battle"))

    def test_live_and_paused_never_confirm_exit(self):
        """结算遮罩/暂停遮罩还在时，OCR 证据不足以确认退出。"""
        self.assertFalse(ended_exit_allowed("live"))
        self.assertFalse(ended_exit_allowed("paused"))

    def test_unknown_never_confirms_exit(self):
        self.assertFalse(ended_exit_allowed("unknown"))
        self.assertFalse(ended_exit_allowed(""))


class AbandonActionEndedGuardTest(unittest.TestCase):
    def setUp(self):
        self.tasks = load_tasks()

    def node(self, name: str) -> dict:
        self.assertIn(name, self.tasks, f"缺少放弃链节点 {name}")
        return self.tasks[name]

    def test_guard_runs_after_failure_guard_and_before_blind_pause(self):
        entry = self.node("AbandonEntry")["next"]
        for name in ("AbandonResultGuard", "AbandonActionEndedGuard",
                     "AbandonPauseButton", "BattlePause"):
            self.assertIn(name, entry)
        self.assertLess(entry.index("AbandonResultGuard"),
                        entry.index("AbandonActionEndedGuard"),
                        "失败守卫必须优先于通用行动结束守卫")
        self.assertLess(entry.index("AbandonActionEndedGuard"),
                        entry.index("BattlePause"), "结算证据必须先于盲点暂停")
        # 结算页曾被暂停双竖条模板误命中（真实截图实测 0.936）：两道结算闸门都必须
        # 排在活战斗暂停模板之前，否则结果页又会被当成活战斗连点暂停。
        self.assertLess(entry.index("AbandonResultGuard"),
                        entry.index("AbandonPauseButton"),
                        "失败页守卫必须先于活战斗暂停模板")
        self.assertLess(entry.index("AbandonActionEndedGuard"),
                        entry.index("AbandonPauseButton"),
                        "行动结束守卫必须先于活战斗暂停模板")

    def test_failure_guard_keeps_its_semantics(self):
        guard = self.node("AbandonResultGuard")
        self.assertEqual(guard["text"], ["任务失败", "任務失敗"])
        self.assertEqual(guard["next"], ["MarkAbandonResultPage"])
        self.assertEqual(guard["exceededNext"], ["BattlePause"])

    def test_ended_guard_reads_generic_settlement_titles(self):
        guard = self.node("AbandonActionEndedGuard")
        self.assertEqual(guard["algorithm"], "OcrDetect")
        self.assertEqual(guard["action"], "DoNothing", "识别节点不能点任何按钮")
        for needle in GENERIC_SETTLEMENT_TITLES:
            self.assertIn(needle, guard["text"])
        self.assertNotIn("任务失败", guard["text"], "失败语义只属于 AbandonResultGuard")
        self.assertEqual(guard["next"], ["MarkAbandonActionEnded"])
        self.assertIn("BattlePause", guard.get("exceededNext", []),
                      "未命中时要回到原来的盲点暂停保底路径")
        self.assertIn("BattlePause", guard.get("onErrorNext", []))

    def test_guard_roi_covers_settlement_title(self):
        """结算页素材上 ROI 必须完整覆盖左侧标题带（失败页、通关页与灰色结算页）。"""
        x, y, width, height = self.node("AbandonActionEndedGuard")["roi"]
        for asset in ("result-failed.png", "result-cleared.png",
                      "result-ended-grey.png"):
            frame = Image.open(ASSETS / asset).convert("RGB").resize((1280, 720))
            rgb = np.asarray(frame, dtype=np.int16)
            white = ((rgb[:, :, 0] >= 245) & (rgb[:, :, 1] >= 245)
                     & (rgb[:, :, 2] >= 245))
            rows = np.nonzero(white[:, x:x + width].sum(axis=1) >= 40)[0]
            title = rows[(rows >= 150) & (rows < 450)]
            self.assertTrue(title.size, f"{asset} 左侧未见结算标题")
            self.assertGreaterEqual(int(title.min()), y, f"{asset} 标题上沿被 ROI 切掉")
            self.assertLessEqual(int(title.max()), y + height, f"{asset} 标题下沿被 ROI 切掉")

    def test_grey_settlement_title_matches_a_needle(self):
        """合成灰色结算页 result-ended-grey.png 的标题带对应「行动结果」。

        离线测试没有 OCR 引擎，无法逐字识别图像；这里用素材约定的文字做契约：
        两道结算 OCR 的 needle 至少有一个能命中它，否则该页会再次落到盲点
        暂停（旧实现只认「行动结束」）。
        """
        variants = {"行动结果", "行動結果"}
        for name, tasks in (("AbandonActionEndedGuard", load_tasks()),
                            ("ResultWinCheck", load_result_tasks())):
            needles = {str(item).replace(" ", "")
                       for item in tasks[name]["text"]}
            self.assertTrue(needles & variants,
                            f"{name} 不能识别「行动结果」结算页：{sorted(needles)}")

    def test_ended_mark_claims_neither_win_nor_abandon(self):
        mark = self.node("MarkAbandonActionEnded")
        self.assertEqual(mark["action"], "Stop")
        ended_sources = [name for name, node in self.tasks.items()
                         if "MarkAbandonActionEnded" in (node.get("next") or [])]
        self.assertEqual(ended_sources, ["AbandonActionEndedGuard"])
        # 只看行为字段：Docs 是写给人看的说明，本身会提到这些标记名字。
        for name in ("AbandonActionEndedGuard", "MarkAbandonActionEnded"):
            fields = {key: value for key, value in self.tasks[name].items()
                      if key != "Docs"}
            blob = json.dumps(fields, ensure_ascii=False)
            for forged in ("MarkBattleWon", "MarkBattleFailed", "MarkBattleAbandoned"):
                self.assertNotIn(forged, blob, f"{name} 不得伪造 {forged}")


class ResultThreeStarTest(unittest.TestCase):
    """三星视觉证据：通用结算标题（行动结束/任务失败）不能直接判胜。"""

    def raster(self, asset: str) -> Image.Image:
        return Image.open(ASSETS / asset).convert("RGB")

    def test_cleared_page_has_three_blue_medals(self):
        # 三星结算素材：640×360，检测内部归一化到 1280×720。
        self.assertTrue(result_three_star(self.raster("result-cleared.png")))

    def test_fixture_verdicts_follow_the_medals(self):
        """素材直接接到复核结论：三星页 verified，合成灰章页 rejected。"""
        self.assertEqual(
            won_evidence_verdict("not_battle", frame_present=True,
                                 three_star=result_three_star(
                                     self.raster("result-cleared.png"))),
            "verified")
        self.assertEqual(
            won_evidence_verdict("not_battle", frame_present=True,
                                 three_star=result_three_star(
                                     self.raster("result-ended-grey.png"))),
            "rejected")

    def test_failed_page_is_not_a_win(self):
        self.assertFalse(result_three_star(self.raster("result-failed.png")))

    def test_grey_action_ended_page_is_not_a_win(self):
        """合成灰色结算页：三枚灰色奖章，蓝色掩膜一枚都取不到。"""
        frame = self.raster("result-ended-grey.png")
        self.assertEqual(frame.size, (1280, 720))
        self.assertFalse(result_three_star(frame))

    def test_blank_and_abnormal_frames_are_not_wins(self):
        """未知不判胜：黑屏、抓帧失败、尺寸异常一律 False。"""
        self.assertFalse(result_three_star(Image.new("RGB", (1280, 720), (0, 0, 0))))
        self.assertFalse(result_three_star(None))
        self.assertFalse(result_three_star(Image.new("RGB", (32, 18), (0, 0, 0))))

    def test_grey_asset_carries_no_account_info(self):
        """灰章素材只能是标题/奖章裁剪，不能夹带等级、经验或账号信息。"""
        frame = np.asarray(self.raster("result-ended-grey.png"))
        outside = np.ones(frame.shape[:2], dtype=bool)
        outside[200:460, 0:500] = False
        self.assertFalse(frame[outside].any(),
                         "裁剪区以外必须是纯黑，不得复制结算页其他信息")

    def test_ended_evidence_confirms_exit_but_never_wins(self):
        """ended 只证明战斗结束：可作为退出证据，battle 结果绝不能是 won。"""
        self.assertIn("ended", RESULT_PAGE_VERDICTS)
        self.assertEqual(
            exit_decision("not_battle", panel_confirmed=False, result_page="ended"),
            "result_page")
        self.assertEqual(battle_result_verdict(set(), "ended"), "unknown")
        self.assertNotEqual(battle_result_verdict(set(), "ended"), "won")

    def test_won_mapping_prefers_failure_and_needs_the_mark(self):
        """标记映射只认 MarkBattleWon；未复核的标记最终仍不得报 won。"""
        self.assertEqual(battle_result_verdict({"MarkBattleWon"}, None), "won")
        self.assertEqual(verified_native_battle(
            battle_result_verdict({"MarkBattleWon"}, None), None), "unknown")
        self.assertEqual(
            battle_result_verdict({"MarkBattleWon", "MarkBattleFailed"}, "ended"),
            "failed")
        self.assertEqual(battle_result_verdict({"MarkBattleFailed"}, "ended"), "failed")


class WonEvidenceTest(unittest.TestCase):
    """MarkBattleWon 复核契约：只有 not_battle + 有效帧才允许下结论。"""

    def test_three_medals_verify_the_win(self):
        self.assertEqual(
            won_evidence_verdict("not_battle", frame_present=True,
                                 three_star=True), "verified")

    def test_settlement_without_medals_rejects_the_win(self):
        """通用结算页（行动结束/行动结果）没有三枚蓝章：结束但不判胜。"""
        self.assertEqual(
            won_evidence_verdict("not_battle", frame_present=True,
                                 three_star=False), "rejected")

    def test_missing_frame_never_decides(self):
        """抓帧失败/unknown/live/paused 都不能给出结论（标记必须保留）。"""
        for scene in ("unknown", "", "live", "paused"):
            self.assertEqual(
                won_evidence_verdict(scene, frame_present=False,
                                     three_star=False), "unverified")
        self.assertEqual(
            won_evidence_verdict("not_battle", frame_present=False,
                                 three_star=False), "unverified")
        # 活战斗/暂停即使误算出三星图形也不得复核通过。
        for scene in ("live", "paused", "unknown"):
            self.assertEqual(
                won_evidence_verdict(scene, frame_present=True,
                                     three_star=True), "unverified")

    def test_unverified_mark_never_becomes_a_win(self):
        """保留的 MarkBattleWon 最终仍不得产出 won；失败/unknown 不受影响。"""
        for evidence in (None, "unverified", "rejected"):
            self.assertEqual(verified_native_battle("won", evidence), "unknown")
            self.assertNotEqual(verified_native_battle("won", evidence), "won")
        self.assertEqual(verified_native_battle("won", "verified"), "won")
        self.assertEqual(verified_native_battle("failed", "unverified"), "failed")
        self.assertEqual(verified_native_battle("unknown", None), "unknown")


class SelectBattleResultTest(unittest.TestCase):
    """原生胜负优先于漏怪/放弃记账；unknown/ended 不判胜负。"""

    def select(self, **overrides) -> str:
        kwargs = dict(mode="official", formation_no_battle=False,
                      native_battle="unknown", abandoned=False,
                      abandon_reason=None, aborted_on_leak=False,
                      abandon_failed=False)
        kwargs.update(overrides)
        return select_battle_result(**kwargs)

    def test_native_win_survives_leak_and_abandon_bookkeeping(self):
        """已拿到原生 MarkBattleWon 时，漏怪/放弃记账不得改写胜负。"""
        for extra in ({"aborted_on_leak": True},
                      {"abandon_failed": True},
                      {"abandoned": True, "abandon_reason": "leak"}):
            self.assertEqual(self.select(native_battle="won", **extra), "won")

    def test_native_failure_survives_leak_and_abandon_bookkeeping(self):
        for extra in ({"aborted_on_leak": True},
                      {"abandon_failed": True},
                      {"abandoned": True, "abandon_reason": "timeout"}):
            self.assertEqual(self.select(native_battle="failed", **extra), "failed")

    def test_unknown_or_ended_never_counts_as_a_win(self):
        """ended 只证明战斗结束：battle_result_verdict 归为 unknown，不冒充胜利。"""
        self.assertEqual(battle_result_verdict(set(), "ended"), "unknown")
        self.assertEqual(self.select(native_battle="unknown"), "unknown")
        self.assertEqual(self.select(native_battle="unknown", aborted_on_leak=True),
                         "aborted_on_leak")
        self.assertNotIn(self.select(native_battle="unknown", aborted_on_leak=True),
                         {"won", "failed"})

    def test_bookkeeping_order_without_native_verdict(self):
        self.assertEqual(
            self.select(abandoned=True, abandon_reason="timeout",
                        aborted_on_leak=True, abandon_failed=True),
            "abandoned_on_timeout")
        self.assertEqual(self.select(aborted_on_leak=True, abandon_failed=True),
                         "aborted_on_leak")
        self.assertEqual(self.select(abandon_failed=True), "abandon_failed")

    def test_formation_never_reports_a_battle(self):
        self.assertEqual(self.select(mode="formation", formation_no_battle=True,
                                     native_battle="won"), "not_started")
        self.assertEqual(self.select(mode="formation", native_battle="won"), "formed")


class SelectRunStatusTest(unittest.TestCase):
    """独立失败状态不被 native won 抹平；归位失败在无更强失败时可见。"""

    def status(self, **overrides) -> str:
        kwargs = dict(formation_no_battle=False, battle="won",
                      abandon_failed=False, sanity_blocked=False,
                      timed_out=False, runtime_error=None,
                      exit_unconfirmed=False, reposition_error=None,
                      program_status="completed")
        kwargs.update(overrides)
        return select_run_status(**kwargs)

    def test_native_win_with_abandon_failed_is_never_completed(self):
        """native won + ABANDON_FAILED 不得落成 completed（否则 ok 会误判为真）。"""
        status = self.status(battle="won", abandon_failed=True)
        self.assertEqual(status, "ABANDON_FAILED")
        self.assertNotEqual(status, "completed")

    def test_reposition_failure_downgrades_a_clean_win(self):
        self.assertEqual(self.status(reposition_error="PanelRecoveryError: boom"),
                         "REPOSITION_FAILED")
        self.assertNotEqual(self.status(reposition_error="boom"), "completed")

    def test_reposition_failure_does_not_mask_stronger_failures(self):
        self.assertEqual(self.status(abandon_failed=True, reposition_error="boom"),
                         "ABANDON_FAILED")
        self.assertEqual(self.status(timed_out=True, reposition_error="boom"), "timeout")
        self.assertEqual(self.status(sanity_blocked=True, reposition_error="boom"),
                         "SANITY_EXHAUSTED")
        # 战斗本身失败时不拿归位这一附加事实覆盖程序状态。
        self.assertEqual(self.status(battle="failed", reposition_error="boom"),
                         "completed")

    def test_reposition_failure_is_visible_when_battle_is_unknown_or_ended(self):
        """结算只证明战斗结束（ended/unknown）时，归位失败是唯一失败事实。"""
        self.assertEqual(
            self.status(battle="unknown", reposition_error="boom",
                        program_status="completed"), "REPOSITION_FAILED")
        self.assertEqual(
            self.status(battle="unknown", reposition_error="boom",
                        program_status="unknown"), "REPOSITION_FAILED")
        self.assertEqual(
            self.status(battle="ended", reposition_error="boom"),
            "REPOSITION_FAILED")

    def test_reposition_failure_does_not_mask_runtime_failure(self):
        self.assertEqual(
            self.status(battle="unknown", program_status="failed",
                        reposition_error="boom"), "failed")

    def test_formation_only_reposition_failure_is_not_success(self):
        self.assertEqual(
            self.status(formation_no_battle=True, battle="not_started",
                        reposition_error="boom"),
            "REPOSITION_FAILED")


class FailureReasonTest(unittest.TestCase):
    """failure.reason 优先级：放弃失败优先于漏怪；归位失败必须可见。"""

    def reason(self, **overrides) -> str:
        kwargs = dict(abandon_failed=False, aborted_on_leak=False,
                      sanity_blocked=False, reposition_error=None,
                      report_reason=None)
        kwargs.update(overrides)
        return failure_reason(**kwargs)

    def test_abandon_failed_is_visible_over_leak(self):
        """客户端读 reason 必须看到放弃失败；漏怪仍留在 top-level leak。"""
        self.assertEqual(self.reason(abandon_failed=True, aborted_on_leak=True),
                         "abandon_failed")

    def test_reposition_error_is_visible(self):
        self.assertEqual(self.reason(reposition_error="PanelRecoveryError: boom"),
                         "reposition_failed")

    def test_stronger_failure_reasons_win(self):
        self.assertEqual(self.reason(aborted_on_leak=True,
                                     reposition_error="boom"), "leak")
        self.assertEqual(self.reason(sanity_blocked=True,
                                     reposition_error="boom"), "SANITY_EXHAUSTED")
        self.assertEqual(self.reason(report_reason="battle_failed"), "battle_failed")


class ParseMaaEventsStatusTest(unittest.TestCase):
    """收尾清理警告只丢证据：录屏/清单/sidecar 失败不得把胜利判成 failed。"""

    WARNINGS = (
        "录屏拉取失败：adb: error: failed to stat remote object '/sdcard/battle.mp4'",
        "复核清单写入失败：PermissionError: [Errno 13] Permission denied",
        "漏怪证据 sidecar 写入失败：OSError: [Errno 28] No space left on device",
    )

    def parse(self, events: list[dict]) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "maa-events.jsonl"
            path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
                encoding="utf-8")
            return parse_maa_events(path)

    def event(self, message: int, payload: dict) -> dict:
        return {"message": message, "details": json.dumps(payload, ensure_ascii=False)}

    def warnings(self) -> list[dict]:
        return [self.event(*cleanup_warning_event(reason)) for reason in self.WARNINGS]

    def test_cleanup_warnings_do_not_fail_a_completed_run(self):
        events = [
            self.event(2, {"what": "CopilotAction",
                           "details": {"action": "Deploy", "target": "桃金娘",
                                       "action_index": 0}}),
            self.event(10002, {"what": "AllTasksCompleted", "details": {}}),
            *self.warnings(),
        ]
        parsed = self.parse(events)

        self.assertEqual(parsed["program_status"], "completed")
        # 警告不是失败消息，不能混进 errors 去给失败报告当死因。
        self.assertEqual(parsed["errors"], [])
        self.assertEqual(parsed["action_count"], 1)

    def test_cleanup_warnings_alone_leave_status_unknown(self):
        self.assertEqual(self.parse(self.warnings())["program_status"], "unknown")

    def test_warning_with_error_message_number_still_does_not_fail(self):
        """解析器按 what 判定：即使上游把警告写成 -1，也不参与状态。"""
        parsed = self.parse([self.event(-1, {"what": "RunnerWarning",
                                            "why": "录屏拉取失败"})])
        self.assertEqual(parsed["program_status"], "unknown")

    def test_runner_error_still_fails_even_next_to_warnings(self):
        events = [
            self.event(10002, {"what": "AllTasksCompleted", "details": {}}),
            *self.warnings(),
            self.event(-1, {"what": "RunnerError", "why": "MAA 停止异常：RuntimeError: boom"}),
        ]
        parsed = self.parse(events)

        self.assertEqual(parsed["program_status"], "failed")
        self.assertTrue(any("RunnerError" in error for error in parsed["errors"]))


if __name__ == "__main__":
    unittest.main()
