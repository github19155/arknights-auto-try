"""下一关解析测试：活动 zone 内的关卡顺序、解锁条件与进度假设。

全部离线：顺序、剧情/突袭过滤与解锁判定用合成关卡表；
缺表/网络失败用 mock，不读取运行时缓存、不联网。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.planner import intel_access
from src.planner.intel_access import resolve_next_stage

ZONE_A = "act99sre_zone1"
ZONE_B = "act99sre_zone2"


def stage(stage_id, code, zone=ZONE_A, *, name=None, story=False,
          difficulty="NORMAL", unlock=()):
    return {
        "stageId": stage_id, "code": code, "zoneId": zone,
        "name": name or code, "difficulty": difficulty,
        "isStoryOnly": story,
        "unlockCondition": [{"stageId": sid, "completeState": state}
                            for sid, state in unlock],
    }


def table(*stages):
    return {"stages": {s["stageId"]: s for s in stages}}


def codes(items):
    return [item["code"] for item in items]


class SyntheticTableTest(unittest.TestCase):
    """合成表：顺序、剧情过滤、突袭排除与解锁条件判定。"""

    def synthetic(self):
        # 01 → ST(剧情) → 02 → TR → 03，另有突袭 #f# 变体挂在 01 后面。
        return table(
            stage("act99sre_01", "XX-1"),
            stage("act99sre_st01", "XX-ST-1", story=True,
                  unlock=[("act99sre_01", "PASS")]),
            stage("act99sre_02", "XX-2", unlock=[("act99sre_st01", "PASS")]),
            stage("act99sre_tr01", "XX-TR-1", unlock=[("act99sre_02", "PASS")]),
            stage("act99sre_03", "XX-3", unlock=[("act99sre_tr01", "PASS")]),
            stage("act99sre_01#f#", "XX-1", difficulty="FOUR_STAR",
                  unlock=[("act99sre_01", "COMPLETE")]),
        )

    def test_story_only_is_skipped(self):
        passed = {"act99sre_01": "PASS", "act99sre_st01": "PASS"}
        result = resolve_next_stage("XX-1", passed, table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["next"]["code"], "XX-2")
        self.assertEqual(result["next"]["stageId"], "act99sre_02")
        self.assertEqual(result["blocked_by"], [])
        self.assertEqual(result["unlock_condition"][0]["satisfied"], True)

    def test_tutorial_stage_is_kept(self):
        result = resolve_next_stage("XX-2", {"act99sre_02": "PASS"},
                                    table=self.synthetic())
        self.assertEqual(result["next"]["code"], "XX-TR-1")

    def test_raid_variant_never_becomes_next(self):
        result = resolve_next_stage("XX-1", {"act99sre_01": "PASS"},
                                    table=self.synthetic())
        listed = [result["next"], *result["remaining"]]
        self.assertNotIn("#f#", " ".join(item["stageId"] for item in listed))
        self.assertEqual(codes(result["remaining"]), ["XX-TR-1", "XX-3"])
        # 只打了普通关也算通关，突袭不计入链条顺序。
        self.assertEqual(result["next"]["code"], "XX-2")

    def test_raid_code_input_resolves_normal_stage(self):
        result = resolve_next_stage("XX-1#f#", {"act99sre_01": "PASS"},
                                    table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["current"]["stageId"], "act99sre_01")
        self.assertTrue(any("突袭" in warning for warning in result["warnings"]))

    def test_uncleared_current_stays_current(self):
        # 前置已满足但当前关没过：仍然回当前关。
        result = resolve_next_stage("XX-2", {"act99sre_st01": "PASS"},
                                    table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["next"]["stageId"], "act99sre_02")
        self.assertEqual(result["current"]["cleared"], False)
        self.assertEqual(result["blocked_by"], [])
        self.assertEqual(codes(result["remaining"]), ["XX-TR-1", "XX-3"])

    def test_unmet_condition_is_reported(self):
        # 进度给了但没有前置：明确标记未满足，不猜已通关。
        result = resolve_next_stage("XX-1", {"act99sre_01": "PASS"},
                                    table=self.synthetic())
        self.assertTrue(result["ok"], result)
        condition = result["unlock_condition"][0]
        self.assertEqual(condition["stageId"], "act99sre_st01")
        self.assertEqual(condition["code"], "XX-ST-1")
        self.assertEqual(condition["completeState"], "PASS")
        self.assertEqual(condition["satisfied"], False)
        self.assertEqual(result["blocked_by"], ["act99sre_st01"])

    def test_missing_progress_marks_unknown(self):
        result = resolve_next_stage("XX-2", table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["next"]["stageId"], "act99sre_02")
        self.assertIsNone(result["current"]["cleared"])
        self.assertIsNone(result["unlock_condition"][0]["satisfied"])
        self.assertEqual(result["blocked_by"], ["act99sre_st01"])
        self.assertTrue(any("未知" in warning for warning in result["warnings"]))

    def test_progress_accepts_code_and_stage_id(self):
        by_code = resolve_next_stage("XX-1", {"XX-1": "PASS"},
                                     table=self.synthetic())
        self.assertEqual(by_code["next"]["code"], "XX-2")
        passed_set = resolve_next_stage("XX-1", {"act99sre_01"},
                                        table=self.synthetic())
        self.assertEqual(passed_set["next"]["code"], "XX-2")

    def test_branch_is_ordered_by_stage_id(self):
        branched = table(
            stage("act99sre_01", "XX-1"),
            stage("act99sre_02", "XX-2", unlock=[("act99sre_01", "PASS")]),
            stage("act99sre_03", "XX-3", unlock=[("act99sre_01", "PASS")]),
        )
        result = resolve_next_stage("XX-1", {"act99sre_01": "PASS"},
                                    table=branched)
        self.assertEqual(result["next"]["stageId"], "act99sre_02")
        self.assertEqual(codes(result["remaining"]), ["XX-3"])
        self.assertTrue(any("分支" in warning for warning in result["warnings"]))

    def test_story_only_start_advances_with_warning(self):
        result = resolve_next_stage("XX-ST-1", {"act99sre_01": "PASS"},
                                    table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["current"]["stageId"], "act99sre_st01")
        self.assertEqual(result["next"]["code"], "XX-2")
        self.assertTrue(any("剧情关" in warning for warning in result["warnings"]))

    def test_unknown_code_fails(self):
        result = resolve_next_stage("ZZ-99", {"act99sre_01": "PASS"},
                                    table=self.synthetic())
        self.assertFalse(result["ok"])
        self.assertIn("ZZ-99", result["error"])
        self.assertIsNone(result["next"])

    def test_zone_end_reports_error(self):
        result = resolve_next_stage("XX-3", {"act99sre_03": "PASS"},
                                    table=self.synthetic())
        self.assertFalse(result["ok"])
        self.assertIn("XX-3", result["error"])


class MultiZoneChainTest(unittest.TestCase):
    """多 zone 活动：TR 插在普通关之间、剧情关跳过、EX 在独立 zone。"""

    def synthetic(self):
        return table(
            stage("act99sre_01", "XX-1"),
            stage("act99sre_02", "XX-2", unlock=[("act99sre_01", "PASS")]),
            stage("act99sre_tr01", "XX-TR-1", unlock=[("act99sre_02", "PASS")]),
            stage("act99sre_03", "XX-3", unlock=[("act99sre_tr01", "PASS")]),
            stage("act99sre_04", "XX-4", unlock=[("act99sre_03", "PASS")]),
            stage("act99sre_ex01", "XX-EX-1", zone=ZONE_B,
                  unlock=[("act99sre_04", "PASS")]),
            stage("act99sre_ex02", "XX-EX-2", zone=ZONE_B,
                  unlock=[("act99sre_ex01", "PASS")]),
            stage("act99sre_ex03", "XX-EX-3", zone=ZONE_B,
                  unlock=[("act99sre_ex02", "PASS")]),
            stage("act99sre_ex02#f#", "XX-EX-2", zone=ZONE_B,
                  difficulty="FOUR_STAR",
                  unlock=[("act99sre_ex01", "COMPLETE")]),
        )

    def test_training_stage_leads_to_next_normal_stage(self):
        result = resolve_next_stage("XX-TR-1", {"act99sre_tr01": "PASS"},
                                    table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["current"]["stageId"], "act99sre_tr01")
        self.assertEqual(result["next"]["code"], "XX-3")
        self.assertEqual(result["next"]["stageId"], "act99sre_03")
        self.assertEqual(result["next"]["zoneId"], ZONE_A)
        self.assertEqual(result["next"]["name"], "XX-3")
        self.assertTrue(result["next"].keys() >= {"code", "stageId", "name", "zoneId"})
        self.assertEqual(result["blocked_by"], [])

    def test_uncleared_current_returns_itself(self):
        result = resolve_next_stage("XX-3", {"act99sre_tr01": "PASS"},
                                    table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["next"]["code"], "XX-3")
        self.assertEqual(result["blocked_by"], [])

    def test_no_progress_blocks_without_guessing(self):
        result = resolve_next_stage("XX-3", table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["next"]["code"], "XX-3")
        self.assertEqual(result["blocked_by"], ["act99sre_tr01"])
        self.assertTrue(any("进度" in warning for warning in result["warnings"]))

    def test_remaining_stays_in_zone_and_skips_nothing_playable(self):
        result = resolve_next_stage("XX-1", {"act99sre_01": "PASS"},
                                    table=self.synthetic())
        self.assertEqual(codes(result["remaining"]),
                         ["XX-TR-1", "XX-3", "XX-4"])

    def test_ex_zone_chain_never_returns_raid(self):
        result = resolve_next_stage("XX-EX-1", {"act99sre_ex01": "PASS"},
                                    table=self.synthetic())
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["next"]["stageId"], "act99sre_ex02")
        self.assertEqual(result["next"]["zoneId"], ZONE_B)
        listed = [result["next"], *result["remaining"]]
        self.assertFalse([item for item in listed if "#f#" in item["stageId"]])
        self.assertEqual(result["remaining"][-1]["code"], "XX-EX-3")


class TableFailureTest(unittest.TestCase):
    """缺表与网络失败必须显式报错，不静默返回空链条。"""

    def test_missing_table_and_network_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "stage_table.json"
            with mock.patch.object(intel_access, "STAGE_TABLE", missing), \
                    mock.patch.object(intel_access, "ensure_tables",
                                      side_effect=OSError("网络不可用")):
                result = resolve_next_stage("XX-1", {"act99sre_01": "PASS"})
        self.assertFalse(result["ok"])
        self.assertIn("stage_table", result["error"])
        self.assertIn("网络不可用", result["error"])
        self.assertIsNone(result["next"])

    def test_broken_table_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "stage_table.json"
            broken.write_text("{not json", encoding="utf-8")
            with mock.patch.object(intel_access, "STAGE_TABLE", broken), \
                    mock.patch.object(intel_access, "ensure_tables",
                                      side_effect=AssertionError("不应联网")):
                result = resolve_next_stage("XX-1")
        self.assertFalse(result["ok"])
        self.assertIn("stage_table", result["error"])

    def test_table_without_stages_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stage_table.json"
            path.write_text(json.dumps({"runeStageGroups": {}}), encoding="utf-8")
            with mock.patch.object(intel_access, "STAGE_TABLE", path):
                result = resolve_next_stage("XX-1")
        self.assertFalse(result["ok"])
        self.assertIn("stages", result["error"])


if __name__ == "__main__":
    unittest.main()
