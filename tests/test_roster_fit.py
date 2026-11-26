"""角色筛选与参考作业适配回归：实际练度字段、usable、Box 排序、作业 underleveled/fit_score。

全部离线：角色表、Box 与本地作业缓存都在临时目录里合成。
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.planner import intel


def char(name, profession="PIONEER", sub="counsellor", position="MELEE"):
    """最小可部署干员卡：费用/阻挡帧 + 无技能。"""
    return {
        "name": name, "profession": profession, "subProfessionId": sub,
        "position": position, "skills": [], "talents": [], "tokens": None,
        "phases": [{"rangeId": "1-1", "attributesKeyFrames": [{"level": 1, "data": {
            "maxHp": 1000, "atk": 100, "def": 100, "magicResistance": 0.0,
            "cost": 10, "blockCnt": 1, "moveSpeed": 1.0,
            "attackSpeed": 100.0, "baseAttackTime": 1.2}}]}],
    }


def tables(chars):
    return {"character_table": {f"char_{i}": c for i, c in enumerate(chars)},
            "skill_table": {}}


class QuerySpecsTrainingTest(unittest.TestCase):
    def specs(self, box, names, role_filter=""):
        with mock.patch.object(intel, "_read_spec_tables",
                               return_value=(tables([char("甲"), char("乙", "CASTER", "corecaster",
                                                                    "RANGED")]), None)), \
             mock.patch.object(intel, "load_box", return_value=box):
            return intel.query_operator_specs(names, role_filter)

    def test_actual_training_fields(self):
        box = [{"id": "char_0", "name": "甲", "elite": 2, "level": 60, "potential": 3}]
        op = self.specs(box, ["甲"])["operators"][0]
        self.assertEqual((op["elite"], op["level"], op["potential"]), (2, 60, 3))
        self.assertTrue(op["usable"])
        self.assertGreater(op["tier_score"], self.specs(
            [{"id": "char_0", "name": "甲", "elite": 1, "level": 70}], ["甲"]
        )["operators"][0]["tier_score"])

    def test_usable_follows_activity_baseline(self):
        cases = [(2, 1, True), (1, 55, True), (1, 54, False), (0, 90, False)]
        for elite, level, expected in cases:
            with self.subTest(elite=elite, level=level):
                box = [{"id": "char_0", "name": "甲", "elite": elite,
                        "level": level, "potential": 1}]
                op = self.specs(box, ["甲"])["operators"][0]
                self.assertEqual(op["usable"], expected)


    def test_role_filter_keeps_training_fields(self):
        box = [{"id": "char_0", "name": "甲", "elite": 2, "level": 60, "potential": 1},
               {"id": "char_1", "name": "乙", "elite": 2, "level": 90, "potential": 1}]
        result = self.specs(box, None, "先锋")
        self.assertEqual([op["name"] for op in result["operators"]], ["甲"])
        self.assertEqual(result["operators"][0]["elite"], 2)


class RankOperatorsTest(unittest.TestCase):
    def rank(self, box, **kwargs):
        chars = [char("甲"), char("乙", "CASTER", "corecaster", "RANGED"),
                 char("丙", "WARRIOR", "lord"), char("丁", "PIONEER", "tactician")]
        with mock.patch.object(intel, "_read_spec_tables",
                               return_value=(tables(chars), None)), \
             mock.patch.object(intel, "load_box", return_value=box):
            return intel.rank_operators(**kwargs)

    def setUp(self):
        self.box = [
            {"id": "char_0", "name": "甲", "elite": 2, "level": 60, "potential": 4},
            {"id": "char_1", "name": "乙", "elite": 2, "level": 90, "potential": 2},
            {"id": "char_2", "name": "丙", "elite": 1, "level": 1, "potential": 1},
            {"id": "char_3", "name": "丁", "elite": 0, "level": 1, "potential": 1},
        ]

    def test_sorted_by_actual_elite_and_level(self):
        names = [op["name"] for op in self.rank(self.box)]
        self.assertEqual(names, ["乙", "甲", "丙", "丁"])
        self.assertEqual([op["usable"] for op in self.rank(self.box)],
                         [True, True, False, False])

    def test_role_and_branch_filters(self):
        self.assertEqual([op["name"] for op in self.rank(self.box, role="先锋")], ["甲", "丁"])
        self.assertEqual([op["name"] for op in self.rank(self.box, branch="counsellor")], ["甲"])
        self.assertEqual([op["name"] for op in self.rank(self.box, role="近卫")], ["丙"])

    def test_min_elite_and_limit(self):
        self.assertEqual([op["name"] for op in self.rank(self.box, min_elite=2)], ["乙", "甲"])
        self.assertEqual([op["name"] for op in self.rank(self.box, limit=1)], ["乙"])


class ReferenceFitTest(unittest.TestCase):
    def job(self, *opers):
        return {"stage_name": "x", "actions": [{"type": "Deploy", "name": "甲"}],
                "opers": list(opers)}

    def fit(self, job, box):
        return intel._reference_fit(job, {op["name"]: op for op in box})

    def test_stated_requirement_is_enforced(self):
        box = [{"name": "甲", "elite": 2, "level": 60, "potential": 1}]
        full = self.fit(self.job({"name": "甲", "requirements": {
            "elite": 2, "level": 60, "skill_level": 10}}), box)
        self.assertEqual(full["underleveled"], [])
        weak = self.fit(self.job({"name": "甲", "requirements": {
            "elite": 2, "level": 90, "skill_level": 10}}), box)
        self.assertLess(weak["fit_score"], full["fit_score"])
        self.assertEqual([item["name"] for item in weak["underleveled"]], ["甲"])

    def test_promoted_operator_satisfies_lower_elite_requirement(self):
        box = [{"name": "甲", "elite": 2, "level": 1, "potential": 1}]
        fit = self.fit(self.job({"name": "甲", "requirements": {
            "elite": 1, "level": 55, "skill_level": 7}}), box)
        self.assertEqual(fit["underleveled"], [])

    def test_low_level_owned_operator_without_requirements_is_underleveled(self):
        box = [{"name": "甲", "elite": 1, "level": 1, "potential": 1}]
        fit = self.fit(self.job({"name": "甲"}), box)
        self.assertEqual([item["name"] for item in fit["underleveled"]], ["甲"])
        self.assertLess(fit["fit_score"], 1.0)

    def test_unowned_operator_lowers_fit_but_is_not_underleveled(self):
        fit = self.fit(self.job({"name": "甲"}), [])
        self.assertEqual(fit["underleveled"], [])
        self.assertEqual(fit["fit_score"], 0.0)


class CachedReferenceFitTest(unittest.TestCase):
    """离线作业缓存：拥有/缺失与练度适配按 Box 重算，不依赖真实账号。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        refs = self.data / "references" / "XX-1"
        refs.mkdir(parents=True)
        for ref_id, name in (("101", "甲"), ("102", "乙")):
            (refs / f"{ref_id}.json").write_text(json.dumps({
                "stage_name": "act99side_01",
                "opers": [{"name": name}],
                "actions": [{"type": "Deploy", "name": name}],
            }, ensure_ascii=False), encoding="utf-8")
        for target, value in (("DATA", self.data), ("ROOT", self.data)):
            patcher = mock.patch.object(intel, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.stage = {"code": "XX-1", "stageId": "act99side_01"}
        self.box = [{"id": "char_0", "name": "甲", "elite": 1, "level": 1,
                     "potential": 1}]

    def by_id(self):
        refs = intel._cached_references(self.stage, [], take=10,
                                        override_operators=self.box)
        return {ref["id"]: ref for ref in refs}

    def test_owned_low_training_operator_is_underleveled(self):
        ref = self.by_id()[101]
        self.assertEqual(ref["have"], ["甲"])
        self.assertEqual(ref["missing"], [])
        self.assertIn("甲", [item["name"] for item in ref["fit"]["underleveled"]])
        self.assertLess(ref["fit"]["fit_score"], 1.0)
        self.assertEqual(ref["file"], "references/XX-1/101.json")
        self.assertEqual(ref["source"], "cache")

    def test_unowned_operator_is_missing_not_underleveled(self):
        ref = self.by_id()[102]
        self.assertEqual(ref["have"], [])
        self.assertEqual(ref["missing"], ["乙"])
        self.assertEqual(ref["fit"]["underleveled"], [])
        self.assertEqual(ref["fit"]["fit_score"], 0.0)

    def test_network_failure_falls_back_to_local_cache(self):
        with mock.patch.object(intel, "http_json", side_effect=OSError("offline")):
            refs = intel.fetch_references(self.stage, [], take=10,
                                          override_operators=self.box)
        self.assertEqual(sorted(ref["id"] for ref in refs), [101, 102])


if __name__ == "__main__":
    unittest.main()
