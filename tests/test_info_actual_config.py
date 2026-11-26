"""信息接口不把游戏表理论值当成 MAA 账号实际技能配置；全部离线。"""
from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.planner import intel


def fixture_tables():
    phases = [{"rangeId": f"range{phase}", "attributesKeyFrames": [{
        "level": 1, "data": {"cost": 10 + phase, "blockCnt": phase + 1,
                              "respawnTime": 70, "maxHp": 100 + phase}}]}
        for phase in range(3)]
    char = {"name": "甲", "profession": "WARRIOR", "subProfessionId": "lord",
            "position": "MELEE", "rarity": "TIER_6", "phases": phases,
            "skills": [{"skillId": f"skill{i}", "unlockCond": {
                "phase": f"PHASE_{i - 1}", "level": 1}} for i in range(1, 4)]}
    skills = {f"skill{i}": {"levels": [{"name": f"技能{i}",
        "description": f"机制{i} 等级{level}", "duration": level,
        "spData": {"spCost": 30 - level, "initSp": level}} for level in range(1, 11)]}
        for i in range(1, 4)}
    return {"character_table": {"char_a": char}, "skill_table": skills}


def owned(elite=1, level=1):
    return {"id": "char_a", "name": "甲", "own": True, "elite": elite,
            "level": level, "potential": 1, "rarity": 6}


class ActualSpecsTest(unittest.TestCase):
    def setUp(self):
        self.tables = fixture_tables()
        self.table_patch = mock.patch.object(intel, "_read_spec_tables",
                                              return_value=(self.tables, None))
        self.table_patch.start()
        self.addCleanup(self.table_patch.stop)
        ranges = mock.patch.object(intel, "_spec_ranges", return_value={})
        ranges.start()
        self.addCleanup(ranges.stop)

    def test_maa_missing_skills_remain_unknown_and_max_is_separate(self):
        with mock.patch.object(intel, "load_box", return_value=[owned(2, 60)]):
            op = intel.query_operator_specs(["甲"])["operators"][0]
        self.assertTrue(op["owned"])
        self.assertEqual(op["actual"], {"source": "maa", "elite": 2, "level": 60,
            "potential": 1, "skill_level": None, "mastery": None, "module": None})
        self.assertNotIn("M3", json.dumps(op))
        skill = op["skills"][1]
        self.assertEqual((skill["n"], skill["name"], skill["unlocked"]), (2, "技能2", True))
        self.assertIsNone(skill["actual_level"])
        self.assertEqual(skill["level_source"], "maa_missing")
        self.assertEqual(skill["max"], {"sp_cost": 20, "sp_init": 10,
                                        "duration": 10, "desc": "机制2 等级10"})
        self.assertNotIn("sp_cost", skill)

    def test_actual_phase_and_locked_skills_are_not_max_phase(self):
        with mock.patch.object(intel, "load_box", return_value=[owned(0, 1)]):
            op = intel.query_operator_specs(["甲"])["operators"][0]
        self.assertEqual((op["cost"], op["block"]), (10, 1))
        self.assertEqual((op["max"]["cost"], op["max"]["block"]), (12, 3))
        self.assertEqual([s["unlocked"] for s in op["skills"]], [True, False, False])

    def test_unowned_candidates_are_excluded_unless_explicitly_requested(self):
        with mock.patch.object(intel, "load_box", return_value=[]):
            self.assertEqual(intel.query_operator_specs()["operators"], [])
            self.assertEqual(intel.query_operator_specs(["甲"])["operators"], [])
            op = intel.query_operator_specs(["甲"], owned_only=False)["operators"][0]
        self.assertFalse(op["owned"])
        self.assertIsNone(op["actual"]["elite"])
        self.assertIsNone(op["cost"])
        self.assertIsNone(op["skills"][0]["unlocked"])

    def test_all_owned_entries_survive_low_training_or_missing_game_table(self):
        box = [owned(), {"id": "char_new", "name": "新干员", "own": True,
                         "elite": 0, "level": 1, "potential": 2, "rarity": 5},
               {**owned(), "name": "未拥有", "own": False}]
        with mock.patch.object(intel, "load_box", return_value=box):
            roster = intel.own_roster(self.tables)
        by_name = {op["name"]: op for op in roster}
        self.assertEqual(set(by_name), {"甲", "新干员"})
        self.assertFalse(by_name["甲"]["eligible"])
        self.assertEqual(by_name["甲"]["rarity"], 6)
        self.assertEqual(by_name["甲"]["branch"], "lord")
        self.assertEqual(by_name["新干员"]["potential"], 2)


class FitUnknownTest(unittest.TestCase):
    def test_unknown_skill_does_not_count_as_satisfied(self):
        job = {"opers": [{"name": "甲", "requirements": {
            "elite": 2, "level": 60, "skill_level": 10}}]}
        fit = intel._reference_fit(job, {"甲": owned(2, 60)})
        self.assertEqual((fit["satisfied"], fit["total"], fit["fit_score"]), (2, 3, 0.667))
        self.assertFalse(fit["complete"])
        self.assertEqual([(u["name"], u["field"]) for u in fit["unknown"]], [("甲", "skill_level")])
        self.assertEqual(fit["underleveled"], [])

    def test_low_owned_operator_has_both_training_failure_and_skill_unknown(self):
        fit = intel._reference_fit({"opers": [{"name": "甲"}]}, {"甲": owned()})
        self.assertEqual([r["name"] for r in fit["underleveled"]], ["甲"])
        self.assertEqual([r["field"] for r in fit["unknown"]], ["skill_level"])
        self.assertFalse(fit["complete"])
        self.assertEqual(fit["fit_score"], 0.0)


class CompactAccountTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / "levels").mkdir()
        (self.data / "operators.json").write_text(json.dumps({"source": "local",
            "syncTime": "2026-01-02T03:04:05+08:00", "own_opers": [owned()]}), encoding="utf-8")
        self.stage = {"code": "TEST", "stageId": "test", "name": "测试",
            "stageType": "MAIN", "apCost": 0, "difficulty": "NORMAL", "story_only": False}
        cached = {"tiles": {"cells": [], "width": 0, "height": 0},
                  "timeline": [], "bosses": [], "routes": []}
        (self.data / "levels" / "TEST-intel.json").write_text(json.dumps(cached), encoding="utf-8")
        refs = self.data / "references" / "TEST"
        refs.mkdir(parents=True)
        (refs / "1.json").write_text(json.dumps({"stage_name": "test", "opers": [{"name": "甲"}],
            "actions": [{"type": "Deploy", "name": "甲"}]}), encoding="utf-8")
        profile = {"fixed_squad": False, "predefined_ops": []}
        for patch in [mock.patch.object(intel, "DATA", self.data),
                      mock.patch.object(intel, "ROOT", self.data),
                      mock.patch.object(intel, "resolve_stage", return_value=self.stage),
                      mock.patch.object(intel, "resolve_stage_profile", return_value=profile),
                      mock.patch.object(intel, "_mechanics_intel", return_value=("", None, {})),
                      mock.patch.object(intel, "http_json", side_effect=OSError("offline")),
                      mock.patch.object(intel, "data_version", return_value={})]:
            patch.start()
            self.addCleanup(patch.stop)
        self.tables = {**fixture_tables(), "stage_table": {"stages": {"test": {}}}}

    def build(self):
        return intel.build_compact_intel("TEST", tables=self.tables)

    def test_compact_keeps_owned_counts_roster_and_reference_fit(self):
        result = self.build()
        self.assertEqual(result["box"], {"owned": 1, "eligible": 0, "source": "maa",
            "sync_time": "2026-01-02T03:04:05+08:00"})
        self.assertEqual([r["name"] for r in result["roster"]], ["甲"])
        ref = result["references"][0]
        self.assertEqual((ref["have"], ref["missing"]), (["甲"], []))
        self.assertEqual([r["name"] for r in ref["fit"]["underleveled"]], ["甲"])
        self.assertFalse(ref["fit"]["complete"])

    def test_story_query_still_returns_account_roster(self):
        self.stage["story_only"] = True
        result = self.build()
        self.assertEqual(result["box"]["owned"], 1)
        self.assertEqual(result["box"]["eligible"], 0)
        self.assertEqual([op["name"] for op in result["roster"]], ["甲"])
        self.assertEqual(result["references"], [])

    def test_override_box_has_no_maa_sync_time(self):
        result = intel.build_compact_intel("TEST", tables=self.tables, override_operators=[owned(2, 1)])
        self.assertEqual(result["box"], {"owned": 1, "eligible": 1,
                                         "source": "override", "sync_time": None})

    def test_mcp_returns_compact_roster_with_sync_metadata_and_fit(self):
        with mock.patch("os.dup2"), mock.patch.object(sys, "stdout", io.StringIO()):
            server = importlib.import_module("mcp_server")
        with mock.patch.object(server, "_stage_intel", side_effect=lambda _: self.build()):
            result = server.get_box_and_references("TEST")
        self.assertEqual(result["box"]["sync_time"], "2026-01-02T03:04:05+08:00")
        op = result["roster"][0]
        self.assertLessEqual({"id", "name", "elite", "level", "potential", "rarity",
                              "prof", "branch", "position", "eligible"}, op.keys())
        self.assertNotIn("skills", op)
        self.assertIn("unknown", result["references"][0]["fit"])


if __name__ == "__main__":
    unittest.main()
