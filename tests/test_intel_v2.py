"""intel v2 契约测试：敌人等级变体、roster 名称回退、地图规则透传、交叉校验与产物结构。

全部离线：敌人表、关卡数据、角色表与缓存目录都在临时目录里合成。
"""
from __future__ import annotations

import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.planner import intel
from src.planner.intel import (
    build_timeline,
    compute_kill_total,
    cross_check_kill_total,
    data_version,
    enemy_index,
    enemy_stats,
    enemy_summary,
    own_roster,
    parse_options,
    parse_runes,
    parse_tiles,
)


def wrap(value):
    """enemy_database 的字段包装；m_defined=false 表示该变体不覆盖此字段。"""
    return {"m_defined": True, "m_value": value}


def unwrapped(value):
    return {"m_defined": False, "m_value": value}


def enemy_variant(level, *, name="测试敌人", hp=100, atk=10, level_type="NORMAL",
                  immunes=(), attributes=None):
    stats = {
        "maxHp": wrap(hp), "atk": wrap(atk), "def": wrap(0),
        "magicResistance": wrap(0.0), "massLevel": wrap(1), "blockCnt": wrap(1),
        "moveSpeed": wrap(0.7), "baseAttackTime": wrap(1.5), "attackSpeed": wrap(100.0),
        "epResistance": wrap(0.0), "epDamageResistance": wrap(0.0),
    }
    for field in immunes:
        stats[field] = wrap(True)
    if attributes:
        stats.update(attributes)
    return {"level": level, "enemyData": {
        "name": wrap(name), "prefabKey": wrap(f"prefab_{level}"),
        "levelType": wrap(level_type), "lifePointReduce": wrap(1),
        "description": wrap("<@ba.vup>描述</>"), "attributes": stats, "skills": None,
    }}


def operator_char(frames=None, skills=None, profession="CASTER"):
    return {
        "name": "干员甲", "profession": profession, "position": "RANGED",
        "phases": [{"rangeId": "3-6",
                    "attributesKeyFrames": frames or [{"level": 1, "data": {
                        "maxHp": 958, "atk": 276, "def": 48, "magicResistance": 10.0,
                        "cost": 18, "blockCnt": 1, "moveSpeed": 1.0,
                        "attackSpeed": 100.0, "baseAttackTime": 1.6}}]}],
        "skills": skills or [], "talents": [], "tokens": None,
    }


def skill_levels():
    return [{"name": f"技能{i + 1}", "description": "desc", "skillType": "MANUAL",
             "duration": float(i), "spData": {"spType": "INCREASE_WITH_TIME", "spCost": i}}
            for i in range(10)]


class EnemyVariantTest(unittest.TestCase):
    def setUp(self):
        self.idx = enemy_index({"enemies": [
            {"Key": "enemy_a", "Value": [enemy_variant(0, hp=100),
                                         enemy_variant(1, hp=250, atk=40)]},
        ]})

    def test_level_variant_is_selected(self):
        stats = enemy_stats(self.idx, "enemy_a", 1)
        self.assertEqual((stats["level"], stats["maxHp"], stats["atk"]), (1, 250, 40))
        self.assertTrue(stats["found"])
        self.assertEqual(stats["levelType"], "NORMAL")
        self.assertEqual(stats["description"], "描述")

    def test_missing_level_falls_back_and_warns(self):
        warnings = []
        stats = enemy_stats(self.idx, "enemy_a", 7, warnings=warnings)
        self.assertEqual((stats["level"], stats["maxHp"]), (0, 100))
        self.assertTrue(any("回落" in message for message in warnings), warnings)

    def test_partial_variant_inherits_base(self):
        base = enemy_variant(0, hp=100, level_type="BOSS", immunes=["stunImmune"])
        partial = enemy_variant(1, hp=250, attributes={
            "magicResistance": unwrapped(0.0),
            "massLevel": unwrapped(0),
            "stunImmune": unwrapped(False),
        })
        partial["enemyData"]["levelType"] = unwrapped("NORMAL")
        partial["enemyData"]["name"] = unwrapped(None)
        idx = enemy_index({"enemies": [{"Key": "enemy_b", "Value": [base, partial]}]})
        stats = enemy_stats(idx, "enemy_b", 1)
        self.assertEqual((stats["level"], stats["maxHp"]), (1, 250))
        self.assertEqual(stats["magicResistance"], 0.0)
        self.assertEqual(stats["massLevel"], 1)
        self.assertEqual(stats["levelType"], "BOSS")
        self.assertEqual(stats["name"], "测试敌人")
        self.assertIn("stun", stats["immunes"])

    def test_unknown_key_reports_failure(self):
        warnings = []
        stats = enemy_stats(self.idx, "enemy_missing", 0, warnings=warnings)
        self.assertFalse(stats["found"])
        self.assertIsNone(stats["maxHp"])
        self.assertTrue(any("解析失败" in message for message in warnings), warnings)

    def test_boss_uses_referenced_variant(self):
        idx = enemy_index({"enemies": [{"Key": "enemy_boss", "Value": [
            enemy_variant(0, hp=100, level_type="BOSS"),
            enemy_variant(1, hp=900, level_type="BOSS"),
        ]}]})
        level = {
            "enemyDbRefs": [{"id": "enemy_boss", "level": 1}],
            "waves": [{"fragments": [{"actions": [
                {"actionType": "SPAWN", "key": "enemy_boss", "count": 1}]}]}],
        }
        _, bosses = build_timeline(level, idx)
        self.assertEqual(bosses[0]["hp"], 900)
        self.assertEqual(bosses[0]["level"], 1)
        self.assertEqual(bosses[0]["levelType"], "BOSS")
        self.assertEqual(bosses[0]["res"], 0.0)

    def test_timeline_references_enemy_summary(self):
        level = {
            "enemyDbRefs": [{"id": "enemy_a", "level": 1}],
            "waves": [{"preDelay": 3.0, "fragments": [{"actions": [
                {"actionType": "SPAWN", "key": "enemy_a", "count": 2,
                 "interval": 1.0, "routeIndex": 0},
            ]}]}],
        }
        warnings = []
        rows, _ = build_timeline(level, self.idx, warnings=warnings)
        self.assertEqual(len(rows), 2)
        self.assertEqual(compute_kill_total(rows), 2)
        for row in rows:
            self.assertEqual(row["enemy"], "enemy_a@1")
            self.assertEqual(row["level"], 1)
            self.assertIn("counted", row)
        enemies = enemy_summary(level, self.idx, warnings=warnings)
        self.assertEqual(list(enemies), ["enemy_a@1"])
        self.assertEqual(enemies["enemy_a@1"]["maxHp"], 250)
        self.assertEqual(warnings, [])


class RosterFallbackTest(unittest.TestCase):
    def tables(self, chars):
        return {"character_table": chars, "skill_table": {}}

    def test_trap_entry_never_answers_name_fallback(self):
        chars = {"trap_900_test": {"name": "干员甲", "profession": "TRAP",
                                     "position": "NONE"}}
        warnings = []
        roster = own_roster(self.tables(chars), warnings=warnings,
                            override_operators=[{"id": "char_900_variant",
                                                 "name": "干员甲", "elite": 2, "level": 58}])
        self.assertEqual(roster, [])
        self.assertTrue(any("名称回退失败" in message for message in warnings), warnings)

    def test_name_fallback_lands_on_playable_form(self):
        chars = {
            "trap_900_test": {"name": "干员甲", "profession": "TRAP",
                                "position": "NONE"},
            "char_002_alpha": operator_char(),
        }
        warnings = []
        roster = own_roster(self.tables(chars), warnings=warnings,
                            override_operators=[{"id": "char_900_variant",
                                                 "name": "干员甲", "elite": 2, "level": 58}])
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0]["id"], "char_002_alpha")
        self.assertEqual(roster[0]["prof"], "术师")
        self.assertTrue(any("名称回退" in message for message in warnings), warnings)

    def test_stats_follow_actual_elite_and_level(self):
        frames = [{"level": 1, "data": {
            "maxHp": 900, "atk": 100, "def": 50, "magicResistance": 10.0, "cost": 18,
            "blockCnt": 1, "moveSpeed": 1.0, "attackSpeed": 100.0,
            "baseAttackTime": 1.6}}]
        late = [{"level": 1, "data": {
            "maxHp": 1200, "atk": 200, "def": 80, "magicResistance": 15.0, "cost": 20,
            "blockCnt": 1, "moveSpeed": 1.0, "attackSpeed": 100.0,
            "baseAttackTime": 1.6}},
            {"level": 70, "data": {
                "maxHp": 1400, "atk": 240, "def": 90, "magicResistance": 15.0, "cost": 20,
                "blockCnt": 1, "moveSpeed": 1.0, "attackSpeed": 100.0,
                "baseAttackTime": 1.6}}]
        char = operator_char(frames=late)
        char["phases"] = [{"attributesKeyFrames": frames}, {"attributesKeyFrames": late}]
        roster = own_roster({"character_table": {"char_x": char}, "skill_table": {}},
                            override_operators=[{"id": "char_x", "name": "干员甲",
                                                 "elite": 1, "level": 60}])
        self.assertEqual(roster[0]["elite"], 1)
        self.assertEqual(roster[0]["maxHp"], 1200)
        self.assertEqual(roster[0]["magicResistance"], 15.0)



class MapRulesTest(unittest.TestCase):
    def test_options_and_runes_pass_through(self):
        level = {
            "options": {"characterLimit": 8, "maxLifePoint": 3, "initialCost": 10,
                        "maxCost": 99, "costIncreaseTime": 1.0, "unused": True},
            "runes": [{"key": "env_system_new", "difficultyMask": "ALL",
                       "professionMask": 1023, "buildableMask": "ALL",
                       "blackboard": [{"key": "tile.stun", "value": 5.0, "valueStr": None}]}],
        }
        self.assertEqual(parse_options(level), {
            "characterLimit": 8, "maxLifePoint": 3, "initialCost": 10,
            "maxCost": 99, "costIncreaseTime": 1.0})
        self.assertEqual(parse_runes(level), [{
            "key": "env_system_new", "difficultyMask": "ALL",
            "blackboard": [{"key": "tile.stun", "value": 5.0, "valueStr": None}],
            "professionMask": 1023, "buildableMask": "ALL"}])

    def test_empty_level_has_empty_rules(self):
        self.assertEqual(parse_options({}), {field: None for field in (
            "characterLimit", "maxLifePoint", "initialCost", "maxCost", "costIncreaseTime")})
        self.assertEqual(parse_runes({}), [])


class KillTotalCheckTest(unittest.TestCase):
    def test_mismatch_writes_warning(self):
        warnings = []
        check = cross_check_kill_total(38, 36, warnings)
        self.assertFalse(check["ok"])
        self.assertEqual((check["cached"], check["recomputed"]), (38, 36))
        self.assertEqual(len(warnings), 1)
        self.assertIn("交叉校验失败", warnings[0])

    def test_match_and_no_cache_are_clean(self):
        warnings = []
        self.assertTrue(cross_check_kill_total(36, 36, warnings)["ok"])
        self.assertTrue(cross_check_kill_total(None, 36, warnings)["ok"])
        self.assertEqual(warnings, [])


class TileContractTest(unittest.TestCase):
    """tiles 结构契约：旧字段齐全、两用地块同时进 melee/ranged。"""

    def setUp(self):
        self.tiles = parse_tiles({
            "mapData": {
                "map": [[0, 1], [2, 3]],
                "tiles": [
                    {"tileKey": "tile_floor", "heightType": "LOWLAND",
                     "buildableType": "MELEE", "passableMask": "WALK_ONLY"},
                    {"tileKey": "tile_trench", "heightType": "LOWLAND",
                     "buildableType": "RANGED", "passableMask": "WALK_ONLY"},
                    {"tileKey": "tile_road", "heightType": "LOWLAND",
                     "buildableType": "ALL", "passableMask": "WALK_ONLY"},
                    {"tileKey": "tile_start", "heightType": "LOWLAND",
                     "buildableType": "NONE", "passableMask": "WALK_ONLY"},
                ],
            },
        })

    def test_cells_keep_legacy_fields(self):
        cell = self.tiles["cells"][0]
        for key in ("x", "y", "tileKey", "heightType", "buildableType", "class"):
            self.assertIn(key, cell)
        self.assertEqual((self.tiles["width"], self.tiles["height"]), (2, 2))
        self.assertTrue(any("passable_mask" in cell for cell in self.tiles["cells"]))

    def test_all_tile_is_listed_on_both_sides(self):
        self.assertIn((0, 1), {(c["x"], c["y"]) for c in self.tiles["melee"]})
        self.assertIn((0, 1), {(c["x"], c["y"]) for c in self.tiles["ranged"]})
        self.assertEqual(self.tiles["counts"]["all"], 1)


class DataVersionTest(unittest.TestCase):
    """缓存锚点：内容 sha256 + 写入时间 + 来源 URL；缺表不报错也不臆造。"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cache = Path(tmp.name)
        (self.cache / "character_table.json").write_text(
            json.dumps({"char_a": {"name": "干员甲"}}), encoding="utf-8")
        patcher = mock.patch.object(intel, "CACHE", self.cache)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_anchors_have_hash_time_and_url(self):
        version = data_version()
        self.assertIn("character_table", version)
        self.assertNotIn("skill_table", version, "缺表不应臆造锚点")
        entry = version["character_table"]
        self.assertEqual(len(entry["sha256"]), 64)
        self.assertTrue(entry["url"].startswith("https://"))
        self.assertTrue(entry["url"].endswith("character_table.json"))
        datetime.datetime.fromisoformat(entry["fetched_at"])


if __name__ == "__main__":
    unittest.main()
