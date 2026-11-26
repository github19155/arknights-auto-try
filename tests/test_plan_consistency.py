"""作业动作名单与 opers/groups 一致性：硬校验回归测试。

全部离线：情报、角色表都在临时目录里合成；射程表查询被替换为空，
缺表也不会联网或读取真实运行数据。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.engine import validator
from src.engine.validator import validate_job

STAGE = "act99side_01"
TILES = {"width": 8, "height": 8, "cells": [
    {"x": x, "y": y, "tileKey": "road", "heightType": "LOWLAND",
     "buildableType": "MELEE", "class": "melee"}
    for y in range(8) for x in range(8)
]}


def character(name: str, profession: str = "PIONEER", position: str = "MELEE") -> dict:
    return {
        "name": name, "profession": profession, "subProfessionId": "counsellor",
        "position": position, "phases": [{"rangeId": "1-1",
        "attributesKeyFrames": [{"level": 1, "data": {"maxHp": 1000, "atk": 100,
                                                       "def": 100, "cost": 10}}]}],
        "skills": [], "talents": [], "tokens": None,
    }


CHARACTERS = {
    "char_alpha": character("甲"),
    "char_beta": character("乙", "WARRIOR"),
    "char_trap": character("装置", "TRAP", "NONE"),
    "char_token": character("召唤", "TOKEN", "NONE"),
}


def base_job(actions: list[dict], *, opers=None, groups=None) -> dict:
    return {
        "version": 3,
        "stage_name": STAGE,
        "opers": opers if opers is not None else [{"name": "甲"}],
        "groups": groups or [],
        "actions": actions,
    }


def messages(result: dict, key: str) -> list[str]:
    return [str(item.get("message") or "") for item in result.get(key) or []]


class ValidatorFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.chars_path = self.dir / "character_table.json"
        self.chars_path.write_text(json.dumps(CHARACTERS, ensure_ascii=False),
                                   encoding="utf-8")
        self.ranges_path = self.dir / "range_table.json"
        self.ranges_path.write_text("{}", encoding="utf-8")
        self.intel_path = self.dir / "XX-1-intel.json"
        self.intel = {
            "stage": {"code": "XX-1", "stageId": STAGE},
            "tiles": TILES, "routes": [], "timeline": [], "profile": {},
            "predefines_devices": [],
        }
        patches = [
            mock.patch.object(validator, "CHARS", self.chars_path),
            mock.patch.object(validator, "RANGES", self.ranges_path),
            mock.patch.object(validator, "load_box",
                              lambda override_operators=None: list(override_operators or [])),
            mock.patch.object(validator, "load_attack_ranges", return_value={}),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def validate(self, job: dict) -> dict:
        self.intel_path.write_text(json.dumps(self.intel, ensure_ascii=False),
                                   encoding="utf-8")
        return validate_job(job, intel_path=str(self.intel_path))


class DeclaredRosterTest(ValidatorFixture):
    def test_deployed_operators_must_be_declared(self) -> None:
        job = base_job([
            {"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
            {"type": "Deploy", "name": "乙", "location": [1, 2], "direction": "Right"},
            {"type": "Deploy", "name": "装置", "location": [4, 5], "direction": "None"},
        ])

        result = self.validate(job)

        self.assertFalse(result["ok"])
        joined = "\n".join(messages(result, "errors"))
        self.assertIn("乙", joined)
        self.assertIn("opers/groups", joined)
        self.assertNotIn("装置", joined, "character_table 的 TRAP 属于系统装置")

    def test_missing_operators_are_reported_once_each(self) -> None:
        job = base_job([
            {"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
            {"type": "Deploy", "name": "乙", "location": [1, 2], "direction": "Right"},
            {"type": "Deploy", "name": "乙", "location": [1, 3], "direction": "Right"},
        ])

        result = self.validate(job)

        hits = [message for message in messages(result, "errors") if "乙" in message]
        self.assertEqual(len(hits), 1, hits)

    def test_corrected_job_with_declared_operators_passes(self) -> None:
        job = base_job([
            {"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
            {"type": "Deploy", "name": "乙", "location": [1, 2], "direction": "Right"},
        ], opers=[{"name": "甲"}, {"name": "乙"}])

        result = self.validate(job)

        self.assertTrue(result["ok"], messages(result, "errors"))
        self.assertEqual(result["errors"], [])


class RosterResolutionTest(ValidatorFixture):
    def test_group_candidate_resolves_action_name(self) -> None:
        job = base_job(
            [{"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
             {"type": "Deploy", "name": "乙", "location": [1, 2], "direction": "Right"}],
            groups=[{"name": "副队", "opers": [{"name": "乙"}]}],
        )

        result = self.validate(job)

        self.assertTrue(result["ok"], messages(result, "errors"))

    def test_group_name_as_target_resolves(self) -> None:
        job = base_job(
            [{"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
             {"type": "Deploy", "name": "副队", "location": [1, 2], "direction": "Right"}],
            groups=[{"name": "副队", "opers": [{"name": "乙"}]}],
        )

        result = self.validate(job)

        self.assertTrue(result["ok"], messages(result, "errors"))

    def test_token_and_trap_entities_are_allowed(self) -> None:
        job = base_job(
            [{"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
             {"type": "Deploy", "name": "装置", "location": [4, 5], "direction": "None"},
             {"type": "Deploy", "name": "召唤", "location": [5, 1], "direction": "None"}],
        )

        result = self.validate(job)

        self.assertTrue(result["ok"], messages(result, "errors"))

    def test_unprovable_entity_fails(self) -> None:
        job = base_job(
            [{"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
             {"type": "Deploy", "name": "陌生单位", "location": [1, 2], "direction": "Right"}],
        )

        result = self.validate(job)

        self.assertFalse(result["ok"])
        joined = "\n".join(messages(result, "errors"))
        self.assertIn("陌生单位", joined)
        self.assertIn("无法证明是系统装置", joined)

    def test_retreat_and_speed_actions_do_not_trigger_roster_check(self) -> None:
        job = base_job(
            [{"type": "Deploy", "name": "甲", "location": [3, 3], "direction": "Down"},
             {"type": "Retreat", "name": "乙"},
             {"type": "SpeedUp"},
             {"type": "SkillDaemon"}],
        )

        result = self.validate(job)

        self.assertTrue(result["ok"], messages(result, "errors"))


if __name__ == "__main__":
    unittest.main()
