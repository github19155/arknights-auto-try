"""分层情报接口测试：简报、分段查询、原始回退与 PRTS 敌人视觉解析。

除显式开启的在线 smoke 外全部离线：合成 intel 产物 + 伪造 PRTS JSON，
不依赖真实网络。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest import mock

from src.planner import intel_access
from src.planner.intel_access import (
    enemy_records,
    get_enemy_visual,
    get_stage_brief,
    match_enemy_title,
    parse_enemy_page,
    parse_imageinfo,
    pick_enemy_image,
    query_stage_intel,
)

STAGE = {
    "input": "XX-1", "code": "XX-1", "stageId": "act99side_01",
    "canonical_stage_id": "act99side_01", "cache_key": "XX-1",
    "levelId": "Activities/act99side/level_act99side_01", "name": "测试关",
    "stageType": "ACTIVITY", "apCost": 20, "difficulty": "NORMAL",
    "variants": {"act99side_01": "NORMAL"}, "story_only": False,
}

ENEMIES = {
    "enemy_a": {"key": "enemy_a", "name": "测试敌人", "level": 0, "maxHp": 1000, "atk": 10},
    "enemy_a@1": {"key": "enemy_a", "name": "测试敌人", "level": 1, "maxHp": 2000, "atk": 20},
    "enemy_b": {"key": "enemy_b", "name": "敌人乙", "level": 0, "maxHp": 500, "atk": 5},
    "enemy_c@1": {"key": "enemy_c", "name": "敌人丙", "level": 1, "maxHp": 300, "atk": 3},
    "enemy_c@2": {"key": "enemy_c", "name": "敌人丙", "level": 2, "maxHp": 900, "atk": 9},
    "enemy_d@1": {"key": "enemy_d", "name": "敌人丁", "level": 1, "maxHp": 700, "atk": 7},
}

REFERENCES = [{
    "id": 110030, "uploader": None, "upload_time": None, "difficulty": 3,
    "title": "五人测试", "opers": ["甲", "乙", "丙"], "have": ["甲"],
    "missing": ["乙", "丙"], "file": "data/references/XX-1/110030.json",
    "source": "cache",
}]


def tiles() -> dict:
    return {
        "width": 4, "height": 2,
        "cells": [{"x": x, "y": y, "tileKey": "tile_floor", "heightType": "LOWLAND",
                   "buildableType": "MELEE", "class": "melee"}
                  for y in range(2) for x in range(4)],
        "melee": [{"x": 0, "y": 0}], "ranged": [{"x": 1, "y": 0}],
        "special": {"spawn": [{"x": 3, "y": 1}], "goal": [{"x": 0, "y": 1}]},
        "counts": {"melee": 1, "ranged": 1, "spawn": 1, "goal": 1},
    }


def full_intel(**overrides) -> dict:
    data = {
        "stage": dict(STAGE), "source": "https://example.invalid/level_act99side_01.json",
        "tiles": tiles(), "maa_check": {"checked": 8, "mismatches": []},
        "routes": [
            {"index": 0, "motion_mode": "WALK", "start": [3, 1], "end": [0, 1],
             "checkpoints": [[2, 1]]},
            {"index": 1, "motion_mode": "WALK", "start": [3, 1], "end": [0, 1],
             "checkpoints": []},
            {"index": 2, "motion_mode": "FLY", "start": [2, 0], "end": [0, 0],
             "checkpoints": []},
        ],
        "timeline": [{"t": 1.0, "wave": 1, "name": "测试敌人", "key": "enemy_a",
                      "enemy": "enemy_a", "level": 0, "levelType": "NORMAL",
                      "route": 0, "counted": True}],
        "bosses": [{"key": "enemy_b", "name": "敌人乙", "levelType": "BOSS", "hp": 500,
                    "atk": 5, "def": 1, "res": 0.0, "lifePointReduce": 3,
                    "description": "不应进入简报的长描述"}],
        "enemies": ENEMIES,
        "options": {"characterLimit": 8, "maxLifePoint": 3, "initialCost": 10,
                    "maxCost": 99, "costIncreaseTime": 1.0},
        "runes": [{"key": "env_x", "difficultyMask": "ALL", "blackboard": []}],
        "roster": [{"name": "甲", "id": "char_a", "elite": 2, "level": 60,
                    "maxHp": 1000}],
        "references": [dict(REFERENCES[0])],
        "box": {"owned": 1, "eligible": 2, "source": "global"},
        "mechanics_text": "机制A" * 300,
        "wiki_source": None,
        "predefines_devices": [{"id": "trap_x", "name": "翻板", "x": 1, "y": 1}],
        "mechanics_source": {"stage": ["机制A"], "activity": []},
        "activity_lookup": None,
        "warnings": ["w1"],
        "kill_total": 1, "kill_total_source": "level_waves",
        "kill_total_check": {"cached": None, "recomputed": 1, "ok": True},
        "story_only": False,
    }
    data.update(overrides)
    return data


def compact_meta(path: Path, *, story_only: bool = False) -> dict:
    return {
        "stage": {key: STAGE[key] for key in (
            "code", "stageId", "name", "stageType", "apCost", "difficulty")},
        "kill_total": 1, "kill_total_source": "level_waves",
        "box": {"owned": 1, "eligible": 2, "source": "global"},
        "story_only": story_only, "story_hint": "纯剧情节点" if story_only else None,
        "mechanics_text": "机制A" * 300, "wiki_source": None,
        "predefines_devices": [{"id": "trap_x", "name": "翻板"}],
        "references": [{key: REFERENCES[0][key] for key in (
            "id", "title", "difficulty", "have", "missing", "file", "source")}],
        "intel_path": str(path), "warnings": ["meta警告"],
        "profile": {}, "predefined_ops": [],
    }


class IntelFixture(unittest.TestCase):
    """把 build_compact_intel 换成读取临时 intel 产物，全程离线。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.intel_path = self.dir / "XX-1-intel.json"
        self.write_intel(full_intel())

    def write_intel(self, data: dict) -> None:
        self.intel_path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                                   encoding="utf-8")

    def patch_meta(self, meta: dict | None = None) -> None:
        patcher = mock.patch.object(intel_access, "build_compact_intel",
                                    return_value=meta or compact_meta(self.intel_path))
        patcher.start()
        self.addCleanup(patcher.stop)


class BriefTest(IntelFixture):
    def test_brief_is_index_only(self) -> None:
        self.patch_meta()
        brief = get_stage_brief("XX-1")

        for key in ("stage", "kill_total", "options", "map", "routes", "bosses",
                    "enemies", "mechanics", "box", "references",
                    "available_sections", "query_hint", "warnings",
                    "raw_path", "source_path", "story_only"):
            self.assertIn(key, brief)
        self.assertEqual(brief["stage"]["code"], "XX-1")
        self.assertEqual(brief["options"]["characterLimit"], 8)
        self.assertEqual((brief["map"]["width"], brief["map"]["height"]), (4, 2))
        self.assertEqual(brief["map"]["counts"]["spawn"], 1)

        self.assertEqual(brief["routes"]["count"], 3)
        self.assertEqual(len(brief["routes"]["pairs"]), 2)
        pair = next(p for p in brief["routes"]["pairs"] if p["routes"] == [0, 1])
        self.assertEqual((pair["start"], pair["end"]), ([3, 1], [0, 1]))

        self.assertEqual([b["name"] for b in brief["bosses"]], ["敌人乙"])
        self.assertNotIn("description", brief["bosses"][0])

        self.assertEqual({entry["key"] for entry in brief["enemies"]}, set(ENEMIES))
        for entry in brief["enemies"]:
            self.assertEqual(set(entry), {"key", "name"})

        self.assertTrue(brief["mechanics"]["truncated"])
        self.assertEqual(len(brief["mechanics"]["summary"]), 400)

        self.assertEqual(brief["references"],
                         [{"id": 110030, "title": "五人测试", "ops": 3,
                           "have": 1, "missing": ["乙", "丙"]}])
        self.assertEqual(brief["available_sections"],
                         ["map", "routes", "timeline", "enemies", "mechanics",
                          "roster", "references", "raw"])
        self.assertIn("query_stage_intel", brief["query_hint"])
        self.assertIn("XX-1", brief["query_hint"])
        self.assertEqual(brief["raw_path"], str(self.intel_path))
        self.assertEqual(brief["source_path"], "https://example.invalid/level_act99side_01.json")
        self.assertIn("w1", brief["warnings"])
        self.assertIn("meta警告", brief["warnings"])

    def test_brief_never_carries_bulk_fields(self) -> None:
        self.patch_meta()
        brief = get_stage_brief("XX-1")
        for bulk in ("timeline", "roster", "tiles", "mechanics_text",
                     "predefines_devices", "runes"):
            self.assertNotIn(bulk, brief)
        self.assertLess(len(json.dumps(brief, ensure_ascii=False)), 4096)

    def test_story_only_brief_lists_only_real_sections(self) -> None:
        story = full_intel(story_only=True)
        for field in ("tiles", "routes", "timeline", "roster"):
            story.pop(field)
        self.write_intel(story)
        self.patch_meta(compact_meta(self.intel_path, story_only=True))

        brief = get_stage_brief("XX-1")
        self.assertTrue(brief["story_only"])
        self.assertEqual(brief["story_hint"], "纯剧情节点")
        self.assertIsNone(brief["map"])
        self.assertEqual(brief["routes"], {"count": 0, "pairs": []})
        self.assertEqual(brief["available_sections"],
                         ["enemies", "mechanics", "references", "raw"])

    def test_missing_artifact_fails_loudly(self) -> None:
        self.intel_path.unlink()
        self.patch_meta()
        with self.assertRaises(FileNotFoundError) as ctx:
            get_stage_brief("XX-1")
        self.assertIn(str(self.intel_path), str(ctx.exception))
        with self.assertRaises(FileNotFoundError):
            query_stage_intel("XX-1", "map")

    def test_formatting_failure_keeps_raw_path_and_warning(self) -> None:
        self.write_intel(full_intel(bosses=[42]))
        self.patch_meta()
        brief = get_stage_brief("XX-1")
        self.assertIsNone(brief["bosses"])
        self.assertTrue(any("bosses 格式化失败" in message and str(self.intel_path) in message
                            for message in brief["warnings"]), brief["warnings"])
        self.assertEqual(brief["raw_path"], str(self.intel_path))


class SectionTest(IntelFixture):
    def setUp(self) -> None:
        super().setUp()
        self.patch_meta()

    def test_map_returns_tiles_and_options(self) -> None:
        result = query_stage_intel("XX-1", "map")
        self.assertEqual(result["section"], "map")
        self.assertEqual(result["stage"]["code"], "XX-1")
        self.assertEqual(result["data"]["tiles"]["width"], 4)
        self.assertEqual(result["data"]["options"]["maxLifePoint"], 3)
        self.assertEqual(result["raw_path"], str(self.intel_path))
        self.assertTrue(result["source_path"])
        self.assertIn("w1", result["warnings"])

    def test_routes_timeline_roster(self) -> None:
        routes = query_stage_intel("XX-1", "routes")
        self.assertEqual([row["index"] for row in routes["data"]], [0, 1, 2])
        timeline = query_stage_intel("XX-1", "timeline")
        self.assertEqual(timeline["data"][0]["enemy"], "enemy_a")
        roster = query_stage_intel("XX-1", "roster")
        self.assertEqual(roster["data"][0]["name"], "甲")

    def test_enemies_full_and_single(self) -> None:
        everything = query_stage_intel("XX-1", "enemies")
        self.assertEqual(set(everything["data"]), set(ENEMIES))
        self.assertIsNone(everything["key"])

        level_one = query_stage_intel("XX-1", "enemies", key="enemy_a@1")
        self.assertEqual(level_one["data"]["maxHp"], 2000)
        self.assertEqual(level_one["matched_by"], "label")
        self.assertEqual(level_one["matched_label"], "enemy_a@1")

        by_base_key = query_stage_intel("XX-1", "enemies", key="enemy_d")
        self.assertEqual(by_base_key["data"]["maxHp"], 700)
        self.assertEqual(by_base_key["matched_by"], "key")

        by_name = query_stage_intel("XX-1", "enemies", key="敌人乙")
        self.assertEqual(by_name["data"]["key"], "enemy_b")
        self.assertEqual(by_name["matched_by"], "name")

    def test_enemy_misses_raise_keyerror(self) -> None:
        with self.assertRaises(KeyError) as missing:
            query_stage_intel("XX-1", "enemies", key="enemy_missing")
        self.assertIn("未命中", str(missing.exception))

        with self.assertRaises(KeyError) as bad_level:
            query_stage_intel("XX-1", "enemies", key="enemy_a@9")
        self.assertIn("未命中", str(bad_level.exception))

        with self.assertRaises(KeyError) as ambiguous_key:
            query_stage_intel("XX-1", "enemies", key="enemy_c")
        self.assertIn("多个", str(ambiguous_key.exception))

        with self.assertRaises(KeyError) as ambiguous_name:
            query_stage_intel("XX-1", "enemies", key="敌人丙")
        self.assertIn("enemy_c@1", str(ambiguous_name.exception))

    def test_mechanics_section_mapping(self) -> None:
        result = query_stage_intel("XX-1", "mechanics")
        self.assertEqual(set(result["data"]),
                         {"runes", "predefines_devices", "mechanics_text"})
        self.assertEqual(result["data"]["runes"][0]["key"], "env_x")
        self.assertEqual(result["data"]["predefines_devices"][0]["id"], "trap_x")
        self.assertTrue(result["data"]["mechanics_text"].startswith("机制A"))

    def test_references_lookup_by_id(self) -> None:
        everything = query_stage_intel("XX-1", "references")
        self.assertEqual(len(everything["data"]), 1)
        single = query_stage_intel("XX-1", "references", key="110030")
        self.assertEqual(single["data"]["id"], 110030)
        with self.assertRaises(KeyError) as missing:
            query_stage_intel("XX-1", "references", key="999")
        self.assertIn("110030", str(missing.exception))

    def test_raw_returns_paths_not_content(self) -> None:
        result = query_stage_intel("XX-1", "raw")
        self.assertEqual(result["data"]["intel_path"], str(self.intel_path))
        self.assertIn("level_path", result["data"])
        self.assertEqual(result["data"]["intel_source"],
                         "https://example.invalid/level_act99side_01.json")
        self.assertNotIn("timeline", result["data"])
        self.assertNotIn("enemies", result["data"])
        self.assertNotIn("intel", result["data"])

    def test_unknown_section_raises_valueerror(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            query_stage_intel("XX-1", "bogus")
        self.assertIn("map", str(ctx.exception))

    def test_unavailable_section_reports_raw_path(self) -> None:
        self.write_intel({key: value for key, value in full_intel().items()
                          if key != "roster"})
        with self.assertRaises(LookupError) as ctx:
            query_stage_intel("XX-1", "roster")
        message = str(ctx.exception)
        self.assertIn("roster", message)
        self.assertIn(str(self.intel_path), message)
        self.assertIn("available_sections", message)


# --------------------------------------------------------------- 视觉解析（纯函数）
def page_payload(title: str, images: list[str], pageimage: str | None = None,
                 missing: bool = False) -> dict:
    page: dict = {"ns": 0, "title": title}
    if missing:
        page.update({"missing": "", "pageid": -1})
    else:
        page["pageid"] = 1
        page["images"] = [{"ns": 6, "title": f"文件:{name}"} for name in images]
        if pageimage is not None:
            page["pageimage"] = pageimage
    return {"query": {"pages": [page]}}


def imageinfo_payload(url: str) -> dict:
    return {"query": {"pages": [{"title": "文件:示例.png",
                                 "imageinfo": [{"url": url,
                                                "descriptionurl": "https://prts.wiki/w/文件:示例.png"}]}]}}


class EnemyRecordParseTest(unittest.TestCase):
    def test_records_unwrap_and_deduplicate(self) -> None:
        db = {"enemies": [
            {"Key": "enemy_a", "Value": [
                {"enemyData": {"name": {"m_defined": True, "m_value": "测试敌人"}}},
                {"enemyData": {"name": {"m_defined": True, "m_value": "测试敌人"}}}]},
            {"Key": "enemy_b", "Value": [
                {"enemyData": {"name": {"m_defined": False, "m_value": None}}},
                {"enemyData": {"name": {"m_defined": True, "m_value": "敌人乙"}}}]},
        ]}
        self.assertEqual(enemy_records(db), [
            {"key": "enemy_a", "name": "测试敌人"},
            {"key": "enemy_b", "name": "敌人乙"},
        ])
        self.assertEqual(enemy_records({}), [])


class VisualParsingTest(unittest.TestCase):
    def test_page_parses_avatar_portrait_and_ignores_icons(self) -> None:
        parsed = parse_enemy_page(page_payload(
            "测试敌人",
            ["图标 领袖 黑.png", "头像 敌人 测试敌人.png", "立绘 测试敌人.png"],
            pageimage="头像_敌人_测试敌人.png"))
        self.assertFalse(parsed["missing"])
        self.assertEqual(parsed["title"], "测试敌人")
        self.assertEqual(parsed["images"], [
            "文件:图标 领袖 黑.png", "文件:头像 敌人 测试敌人.png",
            "文件:立绘 测试敌人.png"])
        self.assertEqual(parsed["avatar_file"], "文件:头像 敌人 测试敌人.png")
        self.assertEqual(parsed["portrait_file"], "文件:立绘 测试敌人.png")

    def test_avg_npc_file_counts_as_portrait(self) -> None:
        parsed = parse_enemy_page(page_payload(
            "源石虫", ["Avg avg npc 1431 1$1.png"]))
        self.assertEqual(parsed["portrait_file"], "文件:Avg avg npc 1431 1$1.png")

    def test_missing_page_has_no_images(self) -> None:
        parsed = parse_enemy_page(page_payload("不存在", [], missing=True))
        self.assertTrue(parsed["missing"])
        self.assertIsNone(parsed["avatar_file"])
        self.assertIsNone(parsed["portrait_file"])
        self.assertEqual(parsed["images"], [])

    def test_empty_payload_raises(self) -> None:
        with self.assertRaises(LookupError):
            parse_enemy_page({"query": {"pages": []}})

    def test_pick_prefers_portrait_then_avatar(self) -> None:
        self.assertEqual(pick_enemy_image({"portrait_file": "P", "avatar_file": "A"}),
                         ("P", "portrait"))
        self.assertEqual(pick_enemy_image({"portrait_file": None, "avatar_file": "A"}),
                         ("A", "avatar"))
        self.assertEqual(pick_enemy_image({"portrait_file": None, "avatar_file": None}),
                         (None, None))

    def test_imageinfo_url(self) -> None:
        self.assertEqual(parse_imageinfo(imageinfo_payload("https://media.prts.wiki/a.png")),
                         "https://media.prts.wiki/a.png")
        self.assertIsNone(parse_imageinfo({"query": {"pages": [{"title": "x"}]}}))
        with self.assertRaises(LookupError):
            parse_imageinfo({"query": {"pages": []}})

    def test_exact_title_matching_rejects_fuzzy(self) -> None:
        self.assertEqual(match_enemy_title("“巨蚯蚓”", ["“巨蚯蚓”", "“巨蚯蚓”/spine"]),
                         "“巨蚯蚓”")
        self.assertEqual(match_enemy_title("巨蚯蚓", ["“巨蚯蚓”"]), "“巨蚯蚓”")
        self.assertIsNone(match_enemy_title("巨蚯蚓", ["大巨蚯蚓", "巨蚯蚓组长"]))
        with self.assertRaises(LookupError):
            match_enemy_title("测试", ["测试", "“测试”"])


class VisualQueryTest(unittest.TestCase):
    """视觉查询走伪造的 PRTS JSON，不触碰真实网络。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cache_dir = Path(self._tmp.name) / "enemy_visual"

        for target, value in (("VISUAL_CACHE", self.cache_dir),):
            patcher = mock.patch.object(intel_access, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.records = [{"key": "enemy_x", "name": "测试敌人"}]
        patcher = mock.patch.object(intel_access, "_local_enemy_records",
                                    return_value=self.records)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.calls: list[dict] = []
        self.page = page_payload(
            "测试敌人", ["头像 敌人 测试敌人.png", "立绘 测试敌人.png"],
            pageimage="头像_敌人_测试敌人.png")
        self.image_url = "https://media.prts.wiki/aa/bb/test-enemy.png"
        self.downloaded = mock.Mock(return_value=b"PNG-BYTES")
        self.download_patcher = mock.patch.object(
            intel_access, "_download_image", self.downloaded)
        self.download_patcher.start()
        self.addCleanup(self.download_patcher.stop)

        self.search = mock.Mock(return_value=["测试敌人组长"])
        self.search_patcher = mock.patch.object(intel_access, "search_prts", self.search)
        self.search_patcher.start()
        self.addCleanup(self.search_patcher.stop)

        self.prts = mock.Mock(side_effect=self.fake_prts)
        self.prts_patcher = mock.patch.object(intel_access, "_prts_json", self.prts)
        self.prts_patcher.start()
        self.addCleanup(self.prts_patcher.stop)

    def fake_prts(self, params: dict) -> dict:
        self.calls.append(dict(params))
        prop = params.get("prop")
        if prop == "images|pageimages":
            return self.page
        if prop == "imageinfo":
            return imageinfo_payload(self.image_url)
        raise AssertionError(f"unexpected PRTS call: {params}")

    def test_success_caches_image_and_metadata(self) -> None:
        result = get_enemy_visual("测试敌人")
        self.assertTrue(result["ok"])
        self.assertEqual(result["name"], "测试敌人")
        self.assertEqual(result["matched_key"], "enemy_x")
        self.assertEqual(result["image_kind"], "portrait")
        self.assertEqual(result["image_url"], self.image_url)
        self.assertEqual(result["avatar_url"], self.image_url)
        self.assertEqual(result["page_url"],
                         "https://prts.wiki/w/" + urllib.parse.quote("测试敌人", safe=""))
        self.assertTrue(result["source"].startswith("prts.wiki"))
        self.assertIn("立绘 测试敌人.png", result["source"])
        self.assertFalse(result["cached"])

        image_path = Path(result["image_path"])
        self.assertTrue(image_path.is_file())
        self.assertEqual(image_path.parent, self.cache_dir)
        self.assertEqual(image_path.read_bytes(), b"PNG-BYTES")
        self.assertTrue(any(self.cache_dir.glob("*.json")))

        second = get_enemy_visual("测试敌人")
        self.assertTrue(second["cached"])
        self.assertEqual(second["image_path"], result["image_path"])

    def test_second_call_hits_cache(self) -> None:
        get_enemy_visual("测试敌人")
        calls_after_first = len(self.calls)
        downloads_after_first = self.downloaded.call_count
        get_enemy_visual("测试敌人")
        self.assertEqual(len(self.calls), calls_after_first)
        self.assertEqual(self.downloaded.call_count, downloads_after_first)

    def test_cache_hit_recomputes_matched_key(self) -> None:
        self.records[:] = [{"key": "enemy_x", "name": "测试敌人"},
                           {"key": "enemy_y", "name": "测试敌人"}]
        ambiguous = get_enemy_visual("测试敌人")
        self.assertTrue(ambiguous["ok"])
        self.assertIsNone(ambiguous["matched_key"])
        self.assertTrue(any("多个敌人 key" in message
                            for message in ambiguous["warnings"]))

        precise = get_enemy_visual("enemy_x")
        self.assertTrue(precise["cached"])
        self.assertEqual(precise["matched_key"], "enemy_x")
        self.assertEqual(precise["query"], "enemy_x")

    def test_refresh_skips_cache(self) -> None:
        get_enemy_visual("测试敌人")
        calls_after_first = len(self.calls)
        refreshed = get_enemy_visual("测试敌人", refresh=True)
        self.assertFalse(refreshed["cached"])
        self.assertGreater(len(self.calls), calls_after_first)
        self.assertGreater(self.downloaded.call_count, 1)

    def test_enemy_key_query_maps_to_display_name(self) -> None:
        result = get_enemy_visual("enemy_x@1")
        self.assertTrue(result["ok"])
        self.assertEqual(result["matched_key"], "enemy_x")
        self.assertEqual(result["name"], "测试敌人")

    def test_unknown_key_is_explicit_error(self) -> None:
        result = get_enemy_visual("enemy_missing")
        self.assertFalse(result["ok"])
        self.assertIn("敌人表", result["error"])
        self.assertIsNone(result["image_path"])
        self.assertIsNone(result["page_url"])
        self.downloaded.assert_not_called()

    def test_fuzzy_search_result_is_rejected(self) -> None:
        self.page = page_payload("测试敌人", [], missing=True)
        result = get_enemy_visual("测试敌人")
        self.assertFalse(result["ok"])
        self.assertIn("完全匹配", result["error"])
        self.assertIn("测试敌人组长", result["error"])
        self.assertIsNone(result["image_path"])
        self.downloaded.assert_not_called()

    def test_page_without_avatar_or_portrait_is_rejected(self) -> None:
        self.page = page_payload("测试敌人", ["图标 领袖 黑.png"],
                                 pageimage="图标_领袖_黑.png")
        result = get_enemy_visual("测试敌人")
        self.assertFalse(result["ok"])
        self.assertIn("头像", result["error"])
        self.assertIsNone(result["image_path"])
        self.downloaded.assert_not_called()

    def test_similar_page_title_is_rejected(self) -> None:
        self.page = page_payload("测试敌人组长",
                                 ["头像 敌人 测试敌人组长.png", "立绘 测试敌人组长.png"])
        result = get_enemy_visual("测试敌人")
        self.assertFalse(result["ok"])
        self.assertIn("不完全一致", result["error"])
        self.downloaded.assert_not_called()

    def test_name_absent_from_local_table_still_resolves_page(self) -> None:
        self.records.clear()
        result = get_enemy_visual("测试敌人")
        self.assertTrue(result["ok"])
        self.assertIsNone(result["matched_key"])
        self.assertTrue(any("敌人表" in message for message in result["warnings"]))

    def test_network_failure_returns_explicit_error(self) -> None:
        self.prts.side_effect = urllib.error.URLError("offline")
        result = get_enemy_visual("测试敌人")
        self.assertFalse(result["ok"])
        self.assertIn("PRTS", result["error"])
        self.assertIn("offline", result["error"])
        self.assertIsNone(result["image_path"])

    def test_download_failure_returns_explicit_error(self) -> None:
        self.downloaded.side_effect = urllib.error.HTTPError(
            self.image_url, 404, "Not Found", None, None)
        result = get_enemy_visual("测试敌人")
        self.assertFalse(result["ok"])
        self.assertIsNone(result["image_path"])
        self.assertTrue(any("404" in message for message in result["warnings"]))


@unittest.skipUnless(os.environ.get("INTEL_ACCESS_ONLINE") == "1",
                     "设置 INTEL_ACCESS_ONLINE=1 运行真实 PRTS smoke")
class PrtsOnlineSmokeTest(unittest.TestCase):
    def test_real_enemy_visual(self) -> None:
        result = get_enemy_visual("“巨蚯蚓”")
        print(json.dumps({key: result.get(key) for key in (
            "ok", "name", "matched_key", "page_url", "image_path", "image_kind",
            "error")}, ensure_ascii=False))
        self.assertTrue(result["ok"], result.get("error"))
        self.assertEqual(result["matched_key"], "enemy_10253_dsjxb_2")
        self.assertTrue(Path(result["image_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
