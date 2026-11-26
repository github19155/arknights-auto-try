#!/usr/bin/env python3
"""Generic pre-battle intel planner: tiles, waves, boss, roster, references.

Usage:
  python cli.py intel <stage_code> [--refresh]
  python -m src.planner.intel <stage_code> [--refresh]

Nothing here is stage specific: the stage code is resolved through MAA
`stages.json` and the unpacked `stage_table.json` at runtime.

Artifacts:
  data/gamedata_cache/*.json        unpacked tables, downloaded once (gitignored)
  data/levels/<cache-key>.json      full stage JSON（canonical normal 用 code，变体用 stageId）
  data/levels/<cache-key>-intel.json 同上的情报缓存
  data/references/<code>/*.json     top 3 prts.maa.plus copilot jobs

坐标约定：mapData.map 行序与 MAA 部署坐标一致，均为自上而下，
格子 (x, y) 对应 map[y][x]；路线 start/end 的 row 则自下而上，
由 parse_routes() 转换为自上而下的坐标。
"""
from __future__ import annotations

import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
CACHE = DATA / "gamedata_cache"
GDATA = "https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/master/zh_CN/gamedata/"
TABLES = {
    "stage_table": "excel/stage_table.json",
    "character_table": "excel/character_table.json",
    "skill_table": "excel/skill_table.json",
    "enemy_database": "levels/enemydata/enemy_database.json",
}
# 版本锚点覆盖的表：比 TABLES 多一张只读射程表，不改变 ensure_tables 的下载集合。
VERSION_TABLES = {**TABLES, "range_table": "excel/range_table.json"}
# level.json 的 options 字段（目标形态 v2 原样透传）。
OPTION_FIELDS = ("characterLimit", "maxLifePoint", "initialCost", "maxCost", "costIncreaseTime")
# 敌人属性全量字段，名字取自 enemyData.attributes。
ENEMY_STATS = ("maxHp", "atk", "def", "magicResistance", "massLevel", "blockCnt",
               "moveSpeed", "baseAttackTime", "attackSpeed", "epResistance", "epDamageResistance")
ENEMY_IMMUNES = ("stunImmune", "silenceImmune", "sleepImmune", "frozenImmune", "levitateImmune",
                 "disarmedCombatImmune", "fearedImmune", "palsyImmune", "attractImmune",
                 "teleportImmune", "groundBoundImmune")
# 干员属性帧字段：表中名字 → intel 输出名，保留游戏表的 camelCase。
OPERATOR_STATS = (("maxHp", "maxHp"), ("atk", "atk"), ("def", "def"),
                  ("magicResistance", "magicResistance"), ("blockCnt", "blockCnt"),
                  ("cost", "cost"), ("baseAttackTime", "baseAttackTime"),
                  ("attackSpeed", "attackSpeed"))
DEFAULT_MAA = Path(os.environ.get("MAA_DIR", "maa"))
PROFESSION = {
    "PIONEER": "先锋", "WARRIOR": "近卫", "TANK": "重装", "SNIPER": "狙击",
    "CASTER": "术师", "MEDIC": "医疗", "SUPPORT": "辅助", "SPECIAL": "特种",
}
SP_TYPE = {
    "INCREASE_WITH_TIME": "自动回复",
    "INCREASE_WITH_ATTACK": "攻击回复",
    "INCREASE_WITH_TAKE_DAMAGE": "受击回复",
    "INCREASE_WITH_NORMAL_ATTACK": "攻击回复",
    "INCREASE_WHEN_ATTACK": "攻击回复",
    "INCREASE_WHEN_TAKEN_DAMAGE": "受击回复",
}
TRIGGER = {"AUTO": "自动触发", "MANUAL": "手动触发", "PASSIVE": "被动", "NONE": "无"}
POSITION = {"MELEE": "地面", "RANGED": "高台"}
# 关卡 wave 动作的 hiddenGroup：三个已知难度互斥，空值属于公共波次。
DIFFICULTY_GROUPS = ("normal", "hard", "easy")


def difficulty_group(stage: dict) -> str:
    """把 stageId 变体/难度映射成 wave hiddenGroup；默认普通。"""
    stage_id = str((stage or {}).get("stageId") or "").lower()
    difficulty = str((stage or {}).get("difficulty") or "").upper()
    if stage_id.startswith("easy_") or difficulty == "EASY":
        return "easy"
    if stage_id.endswith("#f#") or difficulty in ("FOUR_STAR", "HARD"):
        return "hard"
    return "normal"


def http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return resp.read()


def ensure_tables(refresh: bool = False) -> dict:
    CACHE.mkdir(parents=True, exist_ok=True)
    tables = {}
    for name, rel in TABLES.items():
        path = CACHE / f"{name}.json"
        if refresh or not path.exists():
            # 库路径绝不写 stdout：MCP 的 fd 1 是 JSON-RPC，日志只走 stderr。
            print(f"[cache] downloading {name}.json", file=sys.stderr, flush=True)
            path.write_bytes(http_bytes(GDATA + rel))
        tables[name] = json.loads(path.read_text(encoding="utf-8"))
    return tables


def load_maa_stages(maa_dir: Path) -> dict[str, str]:
    path = maa_dir / "resource" / "stages.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[warn] MAA stages.json 读取失败，退回本地表查找：{exc}", file=sys.stderr)
        return {}
    return {s["code"].upper(): s["stageId"] for s in data if s.get("code") and s.get("stageId")}


def resolve_stage(user_input: str, tables: dict, maa_dir: Path) -> dict:
    stages = tables["stage_table"]["stages"]
    code = user_input.strip()
    if code in stages:  # already a stageId such as act55side_ex08
        stage_id = code
    else:
        stage_id = load_maa_stages(maa_dir).get(code.upper())
        if stage_id not in stages:
            stage_id = None
        if stage_id is None:
            matches = [k for k, v in stages.items()
                       if (v.get("code") or "").upper() == code.upper() and v.get("difficulty") == "NORMAL"]
            if not matches:
                raise ValueError(f"stage not found: {user_input}")
            stage_id = next((key for key in matches if key + "#f#" in stages), None)
            if stage_id is None:
                stage_id = next((key for key in matches if stages[key].get("isPredefined") is True), matches[0])
    entry = stages[stage_id]
    variants = {k: v.get("difficulty") for k, v in stages.items()
                if v.get("code") == entry.get("code")}
    # 普通 code 的既有缓存按 canonical normal 复用；其它 stageId 变体独立缓存，
    # 避免 easy_/main_、lt_tr_ 这类同 code 关卡互相覆盖地图与波次。
    normal_ids = [k for k, v in stages.items()
                  if v.get("code") == entry.get("code") and v.get("difficulty") == "NORMAL"]
    canonical = next((key for key in normal_ids if key + "#f#" in stages), None)
    if canonical is None:
        canonical = next((key for key in normal_ids if stages[key].get("isPredefined") is True),
                         normal_ids[0] if normal_ids else stage_id)
    return {
        "input": user_input,
        "code": entry.get("code", user_input),
        "stageId": stage_id,
        "canonical_stage_id": canonical,
        "cache_key": (entry.get("code") or user_input) if stage_id == canonical else stage_id,
        "levelId": entry.get("levelId") or "",
        "name": entry.get("name", ""),
        "stageType": entry.get("stageType", ""),
        "apCost": entry.get("apCost"),
        "difficulty": entry.get("difficulty", "NORMAL"),
        "variants": variants,
        "story_only": bool(entry.get("isStoryOnly") or not entry.get("levelId")
                           or entry.get("stageType") == "st_story"),
    }


def stage_cache_key(stage: dict) -> str:
    """关卡缓存身份：canonical normal 用 code，其它 stageId 变体用 stageId。"""
    key = stage.get("cache_key")
    if key:
        return key
    # 旧情报没有 cache_key：非普通难度只能来自 stageId 变体；普通缓存沿用
    # canonical normal 的 code 文件名。
    stage_id = stage.get("stageId") or stage.get("code") or ""
    if str(stage.get("difficulty") or "NORMAL").upper() != "NORMAL":
        return stage_id
    return stage.get("code") or stage_id


def stage_level_path(stage: dict) -> Path:
    return DATA / "levels" / f"{stage_cache_key(stage)}.json"


def stage_intel_path(stage: dict) -> Path:
    return DATA / "levels" / f"{stage_cache_key(stage)}-intel.json"


def _online_stage(stage_code: str, tables: dict, maa_dir: Path) -> dict:
    """缺关时只定位这一关，不下载全量 stage_table。"""
    code = stage_code.strip().upper()
    stage_id = load_maa_stages(maa_dir).get(code)
    page = None
    cached_path = DATA / "levels" / f"{code}-intel.json"
    if stage_id is None and cached_path.exists():
        saved = json.loads(cached_path.read_text(encoding="utf-8"))
        previous = saved["stage"]
        stage_id = previous["stageId"]
        if previous.get("levelId") or previous.get("story_only"):
            tables["stage_table"]["stages"][stage_id] = {
                **previous, "isStoryOnly": previous.get("story_only", False),
                "isPredefined": (saved.get("profile") or {}).get("fixed_squad", False),
            }
            return resolve_stage(code, tables, maa_dir)
    if stage_id is None:
        from src.planner.prts_stage_client import fetch_stage_page
        page = fetch_stage_page(code)
        stage_id = page["stage_id"]
    if not stage_id:
        raise ValueError(f"无法定位 {code} 的关卡id；已查本地表、MAA stages.json 和 {page['source_url']}")
    story = bool(page and page["story_only"]) or bool(re.search(r"(?:_|-)st\d+$", stage_id, re.I))
    level_id = ""
    if not story:
        if stage_id.startswith("act") and "_" in stage_id:
            activity, suffix = stage_id.split("_", 1)
            level_id = f"activities/{activity}/level_{activity}_{suffix}"
        elif stage_id.startswith("main_"):
            level_id = f"obt/main/level_{stage_id}"
        elif stage_id.startswith("tr_"):
            level_id = f"obt/training/level_{stage_id}"
        elif stage_id.startswith("wk_"):
            level_id = f"obt/weekly/level_weekly_{stage_id.removeprefix('wk_')}"
        else:
            raise ValueError(f"无法推导 {code} ({stage_id}) 的单关 URL；已查本地表和 MAA stages.json")
    tables["stage_table"]["stages"][stage_id] = {
        "code": code, "stageId": stage_id, "levelId": level_id,
        "name": page["stage_name"] if page else code,
        "stageType": "ACTIVITY" if stage_id.startswith("act") else "MAIN",
        "difficulty": "NORMAL", "isStoryOnly": story,
        "isPredefined": bool(re.search(r"(?:_|-)tr\d+$", stage_id, re.I)),
        "apCost": 0 if story or re.search(r"(?:_|-)tr\d+$", stage_id, re.I) else None,
        "description": page["clean_text"] if page else "",
    }
    return resolve_stage(code, tables, maa_dir)


def _predefined_operators(level: dict, characters: dict) -> list[dict]:
    """从关卡预设实例与卡片提取中文名、精英阶段和等级，返回保序去重列表。"""
    predefines = level.get("predefines") or {}
    instances = list(predefines.get("characterInsts") or [])
    instances.extend(card["inst"] for card in predefines.get("characterCards") or [])
    operators = []
    seen = set()
    for item in instances:
        inst = item.get("inst", item)
        name = characters[inst["characterKey"]]["name"]
        if name in seen:
            continue
        seen.add(name)
        op = {"name": name}
        if "phase" in inst:
            op["elite"] = int(str(inst["phase"]).removeprefix("PHASE_"))
        if "level" in inst:
            op["level"] = inst["level"]
        operators.append(op)
    return operators


def resolve_stage_profile(stage_code: str, *, tables=None, maa_dir=None) -> dict:
    """用本地表解析画像；教学关缺 level 缓存时按需下载单关预设。"""
    if tables is None:
        tables = {name: json.loads((CACHE / f"{name}.json").read_text(encoding="utf-8"))
                  for name in ("stage_table", "character_table")}
    directory = Path(maa_dir) if maa_dir is not None else DEFAULT_MAA
    try:
        stage = resolve_stage(stage_code, tables, directory)
    except ValueError:
        stage = _online_stage(stage_code, tables, directory)
    stages = tables["stage_table"]["stages"]
    entry = stages[stage["stageId"]]
    zone = entry.get("zoneId") or ""
    if stage["story_only"]:
        family, notes = "single", "纯剧情节点，无需作战，建议直接跳过"
    elif entry.get("isPredefined") is True:
        family, notes = "tr_fixed", "系统预设编队，零理智通关。"
    elif zone in {f"main_{chapter}" for chapter in range(9, 15)}:
        family, notes = "ep_tuning", "主线调音关，本期仅提供画像。"
    elif zone.startswith(("act2mainss", "act3mainss", "act4mainss")) or entry.get("stageType") == "CLIMB_TOWER":
        family, notes = "ep_hazard_sandbox", "险地沙盘关，本期仅提供画像。"
    elif any("#f#" in key for key in stage["variants"]):
        family, notes = "ex_raid", "突袭关，先普通后突袭。"
    else:
        family, notes = "single", "单模式关卡，普通三星即完成。"
    predefined_ops = []
    if family == "tr_fixed":
        level, _ = fetch_level(stage)
        predefined_ops = _predefined_operators(level, tables["character_table"])
    return {
        "stage_code": stage["code"], "stage_id": stage["stageId"], "family": family,
        "target_modes": ["NORMAL", "RAID"] if family == "ex_raid" else ["NORMAL"],
        "zero_sanity": stage["apCost"] == 0, "fixed_squad": family == "tr_fixed",
        "predefined_ops": predefined_ops, "ap_cost": stage["apCost"],
        "difficulty": stage["difficulty"], "variants": stage["variants"],
        "cache_key": stage_cache_key(stage), "notes": notes,
    }


def level_urls(level_id: str) -> list[str]:
    candidates = []
    if level_id:
        candidates.append(f"levels/{level_id.lower()}.json")
        candidates.append(f"levels/{level_id}.json")
    return candidates


def fetch_level(stage: dict) -> tuple[dict, str]:
    """读取单关 level JSON；本地缺失时按 levelId 下载，全部候选失败抛 RuntimeError。

    不抛 SystemExit：MCP 工具用 ``except Exception`` 结构化返回错误，CLI main
    捕获后打印到 stderr 并返回非零退出码。
    """
    out = stage_level_path(stage)
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raw = out.read_bytes()
        try:
            label = out.relative_to(ROOT).as_posix()
        except ValueError:  # 外部 DATA 目录可能不在仓库内
            label = out.as_posix()
        return json.loads(raw.decode("utf-8")), f"local:{label}"
    errors = []
    for rel in level_urls(stage["levelId"]):
        url = GDATA + rel
        try:
            raw = http_bytes(url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            errors.append(f"{url}: {exc!r}")
            continue
        data = json.loads(raw.decode("utf-8"))
        out.write_bytes(raw)
        return data, rel
    raise RuntimeError("level download failed:\n  " + "\n  ".join(errors))


def tile_class(tile: dict) -> str:
    key = (tile.get("tileKey") or "").replace("tile_", "")
    build = tile.get("buildableType")
    if key in ("start",) or tile.get("isStart"):
        return "spawn"
    if key in ("end",) or tile.get("isEnd"):
        return "goal"
    if key in ("telin", "telout"):
        return "teleport"
    if key in ("hole", "fence_bound", "bigforce"):
        return "blocked"
    if key == "forbidden" or build in ("NONE", 0, None):
        return "floor" if key == "floor" else "forbidden"
    if build in ("ALL", 3):
        return "all"
    if build in ("MELEE", 1) or key == "road":
        return "melee"
    if build in ("RANGED", 2) or key == "trench":
        return "ranged"
    return "other"


def tile_positions(cell: dict) -> set[str]:
    """格子接受的站位类型，优先按原始 buildableType 归一化。

    ALL / 3 是两用地块，MELEE 与 RANGED 都能部署；旧情报曾把 ALL 记成
    class=melee，因此消费者不能只信 class。
    """
    build = cell.get("buildableType")
    if build in ("ALL", 3):
        return {"melee", "ranged"}
    if build in ("MELEE", 1):
        return {"melee"}
    if build in ("RANGED", 2):
        return {"ranged"}
    cls = cell.get("class")
    if cls == "all":
        return {"melee", "ranged"}
    return {cls} if cls in ("melee", "ranged") else set()


def normalized_class(cell: dict) -> str:
    """展示/交叉核对用的单一分类；两用地块统一成 all。"""
    positions = tile_positions(cell)
    if positions == {"melee", "ranged"}:
        return "all"
    if positions:
        return next(iter(positions))
    return cell.get("class") or "other"


def parse_tiles(level: dict) -> dict:
    m = level["mapData"]
    grid = m["map"]
    tiles = m["tiles"]
    height = len(grid)
    cells = []
    for y in range(height):
        for x in range(len(grid[0])):
            tile = tiles[grid[y][x]]
            cell = {
                "x": x, "y": y,
                "tileKey": (tile.get("tileKey") or "").replace("tile_", ""),
                "heightType": tile.get("heightType"),
                "buildableType": tile.get("buildableType"),
                "class": tile_class(tile),
            }
            if "passableMask" in tile:
                cell["passable_mask"] = tile["passableMask"]
            if "playerSideMask" in tile:
                cell["player_side_mask"] = tile["playerSideMask"]
            if tile.get("blackboard"):
                cell["blackboard"] = tile["blackboard"]
            cells.append(cell)
    by_class: dict[str, list] = {}
    for c in cells:
        by_class.setdefault(c["class"], []).append(c)
    # 两用地块同时进入 melee 与 ranged 列表，和图例/validator/verify_with_maa 的
    # 站位语义保持一致；counts 用列表长度而不是互斥分组计数。
    melee = sorted((c for c in cells if "melee" in tile_positions(c)), key=lambda c: (c["y"], c["x"]))
    ranged = sorted((c for c in cells if "ranged" in tile_positions(c)), key=lambda c: (c["y"], c["x"]))
    counts = {k: len(v) for k, v in by_class.items()}
    counts["melee"], counts["ranged"] = len(melee), len(ranged)
    return {
        "width": len(grid[0]), "height": height,
        "cells": cells,
        "melee": melee,
        "ranged": ranged,
        "all": sorted(by_class.get("all", []), key=lambda c: (c["y"], c["x"])),
        "special": {k: sorted(v, key=lambda c: (c["y"], c["x"]))
                    for k, v in by_class.items() if k in ("spawn", "goal", "teleport", "blocked")},
        "counts": counts,
    }


def enemy_index(enemy_db: dict) -> dict:
    return {e["Key"]: e["Value"] for e in enemy_db.get("enemies", [])}


def _unwrap(node):
    """解包 enemy_database 的 m_defined/m_value 包装；普通值原样返回。"""
    if isinstance(node, dict) and "m_value" in node:
        return node["m_value"]
    return node


def _enemy_variants(idx: dict, key: str) -> tuple[str, list | None]:
    """按 key 取等级变体；带序号后缀的 key 缺失时回退到去序号的基础 key。"""
    variants = idx.get(key)
    if variants is not None:
        return key, variants
    base = re.sub(r"_\d+$", "", key)
    return base, idx.get(base)


def _pick_variant(variants: list, level: int | None, key: str,
                  warnings: list | None) -> tuple[dict, int, int]:
    """按 level 选变体，返回 (变体, 实际等级, 下标)；缺变体回落 Value[0] 并记 warning。"""
    levels = [int(v.get("level") or 0) for v in variants]
    if level is not None and level in levels:
        index = levels.index(level)
    else:
        index = 0
        if level is not None and warnings is not None:
            warnings.append(f"敌人 {key} 无 level={level} 变体（可用 {sorted(set(levels))}），"
                            f"回落 level={levels[0]}")
    return variants[index], levels[index], index


def _enemy_name(variants: list, index: int, key: str) -> str:
    """变体自身可能没有名字（m_defined=false），按同一 key 的其它变体回退。"""
    order = [variants[index], *variants[:index], *variants[index + 1:]]
    for variant in order:
        name = _unwrap((variant.get("enemyData") or {}).get("name"))
        if name:
            return name
    return key


def _immune_name(field: str) -> str:
    """stunImmune → stun；保持 SCHEMA.md 的短名口径。"""
    name = field.removesuffix("Immune") or field
    return name[:1].lower() + name[1:]


def _enemy_label(key: str, level: int | None) -> str:
    """enemies 字典的引用键：level 为 0/None 用裸 key，其余 key@level。"""
    return f"{key}@{level}" if level else key


def _inherited(value, fallback):
    """变体里 m_defined=false 的字段不覆盖，继承 level 0 的基值。"""
    if isinstance(value, dict) and "m_value" in value:
        if value.get("m_defined") or fallback is None:
            return value["m_value"]
        return _unwrap(fallback)
    if value is None:
        return _unwrap(fallback) if isinstance(fallback, dict) else fallback
    return value


def _merged_attributes(variant: dict, base: dict) -> dict:
    """逐字段继承 base 的属性，只让 m_defined=true 的变体值生效。"""
    attrs = variant.get("attributes") or {}
    base_attrs = base.get("attributes") or {}
    return {name: _inherited(attrs.get(name), base_attrs.get(name))
            for name in set(attrs) | set(base_attrs)}


def enemy_info(idx: dict, key: str) -> dict:
    """Boss/时间轴用的轻量敌人画像（hp/atk/def/res）；保持既有返回键。"""
    key, variants = _enemy_variants(idx, key)
    if not variants:
        return {"key": key, "name": key, "levelType": "?", "hp": None, "atk": None, "def": None, "res": None}
    ed = variants[0]["enemyData"]
    attrs = ed.get("attributes", {})
    return {
        "key": key,
        "name": _enemy_name(variants, 0, key),
        "levelType": _unwrap(ed.get("levelType")) or "?",
        "hp": _unwrap(attrs.get("maxHp")), "atk": _unwrap(attrs.get("atk")),
        "def": _unwrap(attrs.get("def")),
        "res": _unwrap(attrs.get("magicResistance")),
        "lifePointReduce": _unwrap(ed.get("lifePointReduce")),
        "description": re.sub(r"<[^>]*>", "", _unwrap(ed.get("description")) or "")[:120],
        "notCounted": bool(_unwrap(ed.get("notCountInTotal"))),
    }


def enemy_stats(idx: dict, key: str, level: int | None = None, *,
                warnings: list | None = None) -> dict:
    """敌人全量属性；按 enemyDbRefs[].level 选变体，缺失回落到 Value[0]。

    解析不到的 key 不抛错：返回 found=False 并写 warning，调用方据此可观测。
    """
    resolved, variants = _enemy_variants(idx, key)
    if not variants:
        if warnings is not None:
            warnings.append(f"敌人 key 解析失败：{key} 不在 enemy_database")
        return {"key": resolved, "prefabKey": resolved, "name": resolved, "found": False,
                "level": level, "levelType": "?", "lifePointReduce": None,
                **{field: None for field in ENEMY_STATS},
                "immunities": {field: False for field in ENEMY_IMMUNES}, "immunes": [],
                "skills": [], "notCounted": False, "description": ""}
    variant, variant_level, index = _pick_variant(variants, level, resolved, warnings)
    base_ed = variants[0].get("enemyData") or {}
    ed = variant.get("enemyData") or {}

    def value_of(name):
        return _inherited(ed.get(name), base_ed.get(name))

    attrs = _merged_attributes(ed, base_ed)
    immunities = {name: bool(attrs.get(name)) for name in ENEMY_IMMUNES}
    skills = []
    for skill in value_of("skills") or []:
        skills.append({
            "prefabKey": skill.get("prefabKey"),
            "cooldown": skill.get("cooldown"),
            "initCooldown": skill.get("initCooldown"),
            "spCost": skill.get("spCost"),
            "blackboard": [{"key": item.get("key"), "value": item.get("value"),
                            "valueStr": item.get("valueStr")}
                           for item in skill.get("blackboard") or []],
        })
    return {
        "key": resolved,
        "prefabKey": value_of("prefabKey") or resolved,
        "name": value_of("name") or _enemy_name(variants, index, resolved),
        "found": True,
        "level": variant_level,
        "levelType": value_of("levelType") or "?",
        "lifePointReduce": value_of("lifePointReduce"),
        "description": re.sub(r"<[^>]*>", "", value_of("description") or "")[:120],
        **{name: attrs.get(name) for name in ENEMY_STATS},
        "immunities": immunities,
        "immunes": [_immune_name(name) for name, immune in immunities.items() if immune],
        "skills": skills,
        "notCounted": bool(_unwrap(ed.get("notCountInTotal"))),
    }


def build_timeline(level: dict, idx: dict, difficulty: str = "normal",
                   warnings: list | None = None) -> tuple[list, list]:
    """展开波次出怪；difficulty 只选 hiddenGroup 的 normal/hard/easy 变体。

    空 hiddenGroup 是公共波次；未知组名不是已知难度（例如 raid 的预设激活），
    按实际启用结构保留，避免静默丢事件。默认 normal 兼容旧调用。
    行内 enemy/level 是目标形态 v2 新增：实际使用的敌人 key 与等级变体。
    """
    group = (difficulty or "normal").lower()
    if group not in DIFFICULTY_GROUPS:
        raise ValueError(f"unknown difficulty group: {difficulty}")

    def enabled(node: dict) -> bool:
        hidden = str(node.get("hiddenGroup") or "").lower()
        return not hidden or hidden not in DIFFICULTY_GROUPS or hidden == group

    # 出怪动作的 key 与 enemyDbRefs[].id 一一对应，等级变体只看这张表。
    ref_levels = {ref.get("id"): ref.get("level")
                  for ref in level.get("enemyDbRefs") or [] if ref.get("id")}
    rows = []
    t = 0.0
    for wi, wave in enumerate(level.get("waves", []), start=1):
        t += float(wave.get("preDelay") or 0.0)
        latest = t
        for frag in wave.get("fragments", []):
            if not enabled(frag):
                continue
            ft = t + float(frag.get("preDelay") or 0.0)
            latest = max(latest, ft)
            for act in frag.get("actions", []):
                if act.get("actionType") != "SPAWN" or not enabled(act):
                    continue
                start = ft + float(act.get("preDelay") or 0.0)
                count = int(act.get("count") or 1)
                interval = float(act.get("interval") or 0.0)
                spawn_key = act.get("key", "")
                info = enemy_info(idx, spawn_key)
                stats = enemy_stats(idx, spawn_key, ref_levels.get(spawn_key),
                                    warnings=warnings)
                for i in range(count):
                    rows.append({
                        "t": round(start + i * interval, 1),
                        "wave": wi,
                        "name": info["name"],
                        "key": info["key"],
                        "enemy": _enemy_label(stats["key"], stats["level"]),
                        "level": stats["level"],
                        "levelType": info["levelType"],
                        "route": act.get("routeIndex"),
                        "counted": not info.get("notCounted"),
                    })
                latest = max(latest, start + interval * max(count - 1, 0))
        t = latest
    rows.sort(key=lambda r: (r["t"], r["wave"]))
    bosses = {}
    for row in rows:
        if row["levelType"] == "BOSS" and row["key"] not in bosses:
            stats = enemy_stats(idx, row["key"], row["level"], warnings=warnings)
            bosses[row["key"]] = {
                **enemy_info(idx, row["key"]),
                "hp": stats["maxHp"], "atk": stats["atk"], "def": stats["def"],
                "res": stats["magicResistance"],
                "lifePointReduce": stats["lifePointReduce"], "level": stats["level"],
            }
    return rows, list(bosses.values())


def compute_kill_total(timeline_rows: list[dict]) -> int:
    """按展开后的出怪记录计数，Boss 和精英不另行过滤。"""
    return sum(1 for row in timeline_rows if row.get("counted") is True)


def kill_total_for(stage_code: str) -> int:
    """只读本地情报或关卡波次，不触发下载。"""
    code = stage_code.strip().upper()
    path = DATA / "levels" / f"{code}-intel.json"
    if path.exists():
        intel = json.loads(path.read_text(encoding="utf-8"))
        if "kill_total" in intel:
            return int(intel["kill_total"])
    level = json.loads((DATA / "levels" / f"{code}.json").read_text(encoding="utf-8"))
    enemies = json.loads((CACHE / "enemy_database.json").read_text(encoding="utf-8"))
    timeline, _ = build_timeline(level, enemy_index(enemies))
    return compute_kill_total(timeline)


def enemy_summary(level: dict, idx: dict, warnings: list | None = None) -> dict:
    """按 key 去重汇总本关敌人属性；同 key 不同等级分别存 key@level。"""
    refs = {}
    for ref in level.get("enemyDbRefs") or []:
        if ref.get("id"):
            refs[ref["id"]] = ref.get("level")
    spawn_keys = []
    for wave in level.get("waves") or []:
        for frag in wave.get("fragments") or []:
            for act in frag.get("actions") or []:
                if act.get("actionType") == "SPAWN" and act.get("key"):
                    spawn_keys.append(act["key"])
    # enemyDbRefs 覆盖本关全部援引，出怪动作再兜底；顺序按首次出现稳定。
    enemies = {}
    for key in dict.fromkeys([*refs, *spawn_keys]):
        stats = enemy_stats(idx, key, refs.get(key), warnings=warnings)
        enemies[_enemy_label(key, stats["level"])] = stats
    return enemies


def parse_options(level: dict) -> dict:
    """level.options 的五个节奏字段原样透传。"""
    options = level.get("options") or {}
    return {field: options.get(field) for field in OPTION_FIELDS}


def parse_runes(level: dict) -> list[dict]:
    """关卡 runes 结构化：key/difficultyMask/blackboard 必出，其余字段不丢。"""
    rows = []
    for rune in level.get("runes") or []:
        rows.append({
            "key": rune.get("key"),
            "difficultyMask": rune.get("difficultyMask"),
            "blackboard": rune.get("blackboard") or [],
            **{key: value for key, value in rune.items()
               if key not in ("key", "difficultyMask", "blackboard")},
        })
    return rows


def data_version() -> dict:
    """缓存表版本锚点：内容 sha256 + 缓存写入时间(mtime ISO) + 来源 URL。"""
    version = {}
    for name, rel in VERSION_TABLES.items():
        path = CACHE / f"{name}.json"
        if not path.exists():
            continue
        stat = path.stat()
        version[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "fetched_at": datetime.datetime.fromtimestamp(stat.st_mtime)
            .astimezone().isoformat(timespec="seconds"),
            "url": GDATA + rel,
        }
    return version


def cross_check_kill_total(cached, recomputed: int, warnings: list | None = None) -> dict:
    """旧缓存 kill_total 与波次重算数量交叉校验；不一致只写 warnings，不抛异常。"""
    check = {"cached": cached, "recomputed": recomputed, "ok": True}
    if cached is not None and int(cached) != int(recomputed):
        check["ok"] = False
        if warnings is not None:
            warnings.append(f"kill_total 交叉校验失败：缓存 {cached} ≠ 波次计数 {recomputed}")
    return check


def parse_routes(level: dict) -> list[dict]:
    """保留各 route 的移动检查点；WAIT 的占位 position 不参与路径。"""
    rows = []
    for i, route in enumerate(level.get("routes", [])):
        start, end = route.get("startPosition") or {}, route.get("endPosition") or {}
        h = len(level["mapData"]["map"])
        rows.append({
            "index": i,
            "motion_mode": route.get("motionMode", "WALK"),
            "start": [start.get("col"), h - 1 - start.get("row", 0)],
            "end": [end.get("col"), h - 1 - end.get("row", 0)],
            "checkpoints": [[point["position"]["col"], h - 1 - point["position"]["row"]]
                            for point in (route.get("checkpoints") or [])
                            if point.get("type") == "MOVE" and point.get("position")],
        })
    return rows


def verify_with_maa(stage: dict, tiles: dict, maa_dir: Path):
    """Cross-check our tiles against MAA's own Arknights-Tile-Pos data."""
    # 文件名格式固定为 <stageId>-<服务器>-...，因此按 stageId 精确前缀匹配，
    # 主线和日常/训练关也各有自己的 Tile-Pos，不再只认 activities。
    pattern = str(maa_dir / "resource" / "Arknights-Tile-Pos"
                  / f"{glob.escape(stage['stageId'])}-*.json")
    files = sorted(glob.glob(pattern))
    if not files:
        return None
    try:
        rows = json.loads(Path(files[0]).read_text(encoding="utf-8"))["tiles"]
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"[warn] MAA Tile-Pos 解析失败，跳过交叉核对：{exc}", file=sys.stderr)
        return None

    def category(tile):
        if tile.get("isStart"):
            return "spawn"
        if tile.get("isEnd"):
            return "goal"
        build = tile.get("buildableType")
        return {0: "other", 1: "melee", 2: "ranged", 3: "all", "MELEE": "melee",
                "RANGED": "ranged", "ALL": "all"}.get(build, "other")

    # 旧情报把 ALL 记成 melee，必须按 buildableType 归一化后再比对。
    ours = {(c["x"], c["y"]): normalized_class(c) for c in tiles["cells"]}
    bad = []
    for y, row in enumerate(rows):
        for x, tile in enumerate(row):
            mine = ours.get((x, y))
            mine = "other" if mine in ("floor", "forbidden", "blocked", "teleport", "other") else mine
            theirs = category(tile)
            if mine != theirs:
                bad.append((x, y, mine, theirs))
    return {"checked": len(ours), "mismatches": bad, "file": files[0]}


def load_box(override_operators=None) -> list[dict]:
    """override 为 None 时读取全局 own_opers；否则原样返回传入的干员列表。"""
    if override_operators is not None:
        return override_operators
    path = DATA / "operators.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["own_opers"]


# 当前活动常用练度基准：E2 任意等级，或 E1 达到 55 级；不影响拥有口径。
USABLE_ELITE = 2
USABLE_E1_LEVEL = 55
# 参考作业 requirements 中真正影响可复现性的字段。
REFERENCE_REQUIREMENTS = ("elite", "level", "skill_level")


def _training(op: dict | None) -> dict:
    """Box 条目的实际练度；字段缺失输出 None，不臆造 0。"""
    op = op or {}
    return {
        "elite": None if op.get("elite") is None else int(op["elite"]),
        "level": None if op.get("level") is None else int(op["level"]),
        "potential": None if op.get("potential") is None else int(op["potential"]),
    }


def _is_usable(elite: int | None, level: int | None) -> bool:
    """按当前活动常用基准判定可用：E2 或 E1≥55；未拥有/练度未知一律不可用。"""
    if elite is None or level is None:
        return False
    return elite >= USABLE_ELITE or (elite == 1 and level >= USABLE_E1_LEVEL)


def _tier_score(elite: int | None, level: int | None) -> int | None:
    """练度排序分：精英段优先、同段比等级（等级上限 90，小于 1000）。"""
    if elite is None or level is None:
        return None
    return elite * 1000 + level


def _box_training(*, override_operators=None) -> tuple[dict[str, dict], dict[str, dict]]:
    """Box 的姓名/ID 双索引；同键以首次出现为准。"""
    by_name: dict[str, dict] = {}
    by_id: dict[str, dict] = {}
    for op in load_box(override_operators):
        if op.get("own") is False:
            continue
        if op.get("name"):
            by_name.setdefault(op["name"], op)
        if op.get("id"):
            by_id.setdefault(op["id"], op)
    return by_name, by_id



def _operator_frame(char: dict, elite: int, level: int) -> dict:
    """按实际精英/等级取属性帧；阶段缺帧时回退更低阶段，等级超界取该阶段末帧。"""
    phases = char.get("phases") or []
    for stage_index in range(min(int(elite), len(phases) - 1), -1, -1):
        frames = phases[stage_index].get("attributesKeyFrames") or []
        if not frames:
            continue
        exact = next((frame for frame in frames
                      if int(_last_number(frame.get("level")) or 0) == int(level)), None)
        if exact is not None:
            return exact.get("data") or {}
        lower = [frame for frame in frames
                 if int(_last_number(frame.get("level")) or 0) <= int(level)]
        return (lower[-1] if lower else frames[0]).get("data") or {}
    return {}


def _operator_stats(char: dict, elite: int, level: int) -> dict:
    """干员面板：HP/攻/防/RES/阻挡/费用/攻击间隔；帧缺失时为 None。"""
    data = _operator_frame(char, elite, level)
    if not data:
        return {}
    stats = {out: _last_number(data.get(field)) for field, out in OPERATOR_STATS}
    attack_speed = stats.get("attackSpeed")
    base = stats.get("baseAttackTime")
    stats["attackInterval"] = (round(base * 100 / attack_speed, 3)
                               if base and attack_speed else None)
    return stats


def _talents(char: dict, elite: int, level: int, potential: int | None) -> list[dict]:
    """按实际精英/等级/潜能取已解锁天赋；占位与隐藏天赋不输出。"""
    rows = []
    for talent in char.get("talents") or []:
        unlocked = None
        for candidate in talent.get("candidates") or []:
            condition = candidate.get("unlockCondition") or {}
            phase = int(str(condition.get("phase") or "PHASE_0").removeprefix("PHASE_") or 0)
            need_level = int(_last_number(condition.get("level")) or 1)
            need_potential = int(_last_number(candidate.get("requiredPotentialRank")) or 0)
            if ((phase, need_level) <= (int(elite), int(level))
                    and (potential is None or need_potential <= int(potential))):
                unlocked = candidate
        if unlocked is None or unlocked.get("isHideTalent"):
            continue
        name = unlocked.get("name")
        if not name or name == "？？？":
            continue
        desc = re.sub(r"<[^>]*>", "", unlocked.get("description") or "")
        rows.append({
            "name": name,
            "desc": desc.replace("\\n", " ").strip(),
            "unlock_phase": (unlocked.get("unlockCondition") or {}).get("phase"),
            "unlock_level": (unlocked.get("unlockCondition") or {}).get("level"),
            "potential_rank": unlocked.get("requiredPotentialRank"),
        })
    return rows


def own_roster(tables: dict, *, override_operators=None,
               warnings: list | None = None) -> list[dict]:
    """补全全部已拥有干员；eligible 仅标记常用练度，不过滤低练度账号条目。

    资料缺失的账号干员仍保留；override 缺资料条目跳过并写 warning。
    """
    own = load_box(override_operators)
    chars = tables["character_table"]
    by_name = _playable_by_name(chars)
    skills = tables["skill_table"]
    roster = []
    for op in own:
        if op.get("own") is False:
            continue
        training = _training(op)
        elite, lvl = training["elite"], training["level"]
        char_id = op.get("id")
        char = chars.get(char_id) if char_id else None
        if not char:
            # 只按姓名回退到可部署干员；曾命中 trap_627_xb2amy 把阿米娅解析成陷阱。
            fallback_id, fallback = by_name.get(op.get("name", ""), (None, None))
            if fallback is None:
                if warnings is not None:
                    warnings.append(f"roster 名称回退失败：{op.get('name')}({char_id}) "
                                    f"不在 character_table（已排除 trap/token）")
                if override_operators is not None:
                    continue
                fallback_id, fallback = char_id, {}
            if char_id and fallback and warnings is not None:
                warnings.append(f"roster 名称回退：{char_id} 缺失，按姓名 {op.get('name')} "
                                f"命中 {fallback_id}")
            char_id, char = fallback_id, fallback
        rows = _skill_specs(char, skills, elite=elite, level=lvl)
        tokens = [t for t in (char.get("tokens") or []) if isinstance(t, str)]
        potential = op.get("potential")
        roster.append({
            "name": op.get("name") or char.get("name"),
            "id": char_id,
            "prof": PROFESSION.get(char.get("profession"), char.get("profession")),
            "position": POSITION.get(char.get("position"), char.get("position")),
            **training,
            "rarity": op.get("rarity"),
            "branch": char.get("subProfessionId"),
            "eligible": _is_usable(elite, lvl),
            "skill_level": None, "mastery": None, "module": None,
            **(_operator_stats(char, elite, lvl) if elite is not None and lvl is not None else {}),
            "skills": rows,
            "talents": (_talents(char, elite, lvl, potential)
                        if elite is not None and lvl is not None else []),
            "summons": tokens,
        })
    roster.sort(key=lambda r: (r["prof"] or "", -(r["elite"] or 0), -(r["level"] or 0)))
    return roster


SPEC_TABLES = ("character_table", "skill_table")
SPEC_DIRECTIONS = ("RIGHT", "DOWN", "LEFT", "UP")


def _read_spec_tables() -> tuple[dict, str | None]:
    """只读本地解包表；缺表时返回错误文案而不是下载，调用方据此降级。"""
    tables = {}
    for name in SPEC_TABLES:
        path = CACHE / f"{name}.json"
        if not path.exists():
            return tables, f"缺少本地缓存 {path.name}：请先运行 intel 拉取解包表（本查询不联网）"
        tables[name] = json.loads(path.read_text(encoding="utf-8"))
    return tables, None


def _playable_by_name(chars: dict) -> dict[str, tuple[str, dict]]:
    """姓名反查表；TRAP/TOKEN 同名条目让位给可部署干员，保持首次出现的顺序。

    id 前缀 trap_/token_ 与 profession 双保险：阿米娅曾因此被解析成陷阱。
    """
    index: dict[str, tuple[str, dict]] = {}
    for char_id, char in chars.items():
        name = char.get("name")
        if not name or char_id.startswith(("trap_", "token_")):
            continue
        if char.get("profession") in {"TRAP", "TOKEN"}:
            continue
        index.setdefault(name, (char_id, char))
    return index


def _last_number(value):
    """角色属性可能是数字、数字字符串或升级数组；数组取末项作为满级值。"""
    if isinstance(value, (list, tuple)):
        if not value:
            return None
        value = value[-1]
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return float(text)
    return None


def _promotion_stats(char: dict) -> dict:
    """费用/阻挡/再部署取最高精英阶段末级属性帧；空阶段回退到更低精英。"""
    for phase in reversed(char.get("phases") or []):
        frames = phase.get("attributesKeyFrames") or []
        if frames:
            data = frames[-1].get("data") or {}
            return {
                "cost": _last_number(data.get("cost")),
                "block": _last_number(data.get("blockCnt")),
                "redeploy": _last_number(data.get("respawnTime")),
            }
    return {"cost": None, "block": None, "redeploy": None}


def _range_id(char: dict) -> str | None:
    for phase in reversed(char.get("phases") or []):
        if phase.get("rangeId"):
            return phase["rangeId"]
    return None


def _rotate_offset(point: tuple[int, int], direction: str) -> tuple[int, int]:
    """屏幕坐标系旋转，公式与 validator._coverage_counts 的四向换算一致。"""
    dx, dy = point
    return {
        "RIGHT": (dx, dy),
        "DOWN": (-dy, dx),
        "LEFT": (-dx, -dy),
        "UP": (dy, -dx),
    }[direction]


def _spec_ranges() -> dict[str, set[tuple[int, int]]]:
    """复用 validator 的射程解包与 row 取反；缺表时留空，不触发下载。"""
    from src.engine import validator  # validator 反向依赖本模块，延迟导入避免环

    if not validator.RANGES.exists():
        return {}
    return validator.load_attack_ranges()


def _range_offsets(range_id: str | None,
                   attack_ranges: dict[str, set[tuple[int, int]]]) -> dict[str, list[list[int]]]:
    """按 rangeId 取基础射程并输出四向偏移。"""
    if not range_id:
        return {}
    base = attack_ranges.get(range_id)
    if not base:
        return {}
    origin = (0, 0)
    ordered = ([origin] if origin in base else []) + sorted(
        (point for point in base if point != origin), key=lambda point: (point[1], point[0]))
    offsets = {"默认": [list(point) for point in ordered]}
    for direction in SPEC_DIRECTIONS:
        offsets[direction] = [list(_rotate_offset(point, direction)) for point in ordered]
    return offsets


def _skill_specs(char: dict, skills: dict, *, elite=None, level=None) -> list[dict]:
    """保留所有技能机制；MAA 未提供技能练度，末级效果只放在 max。"""
    rows = []
    for n, slot in enumerate(char.get("skills") or [], start=1):
        skill_id = slot.get("skillId") if isinstance(slot, dict) else None
        levels = ((skills.get(skill_id) or {}).get("levels")) or []
        if not levels:
            continue
        last = levels[-1]
        sp = last.get("spData") or {}
        desc = re.sub(r"<[^>]*>", "", last.get("description") or "").replace("\\n", " ").strip()
        condition = slot.get("unlockCond") or {}
        phase = condition.get("phase")
        unlocked = None
        if elite is not None and phase is not None:
            need_elite = int(str(phase).removeprefix("PHASE_"))
            if elite != need_elite:
                unlocked = elite > need_elite
            elif level is not None and condition.get("level") is not None:
                unlocked = level >= int(condition["level"])
        rows.append({
            "n": n, "name": last.get("name", ""), "unlocked": unlocked,
            "actual_level": None, "level_source": "maa_missing",
            "spType": SP_TYPE.get(sp.get("spType"), sp.get("spType")),
            "trigger": TRIGGER.get(last.get("skillType"), last.get("skillType")),
            "max": {
                "sp_cost": _last_number(sp.get("spCost")),
                "sp_init": _last_number(sp.get("initSp")),
                "duration": _last_number(last.get("duration")),
                "desc": desc,
            },
        })
    return rows


def _matches_role(char: dict, role_filter: str) -> bool:
    """职业过滤同时接受中文职业名、PROFESSION 枚举和 subProfessionId。"""
    role = role_filter.strip()
    if not role:
        return True
    needle = role.casefold()
    profession = char.get("profession")
    return needle in {
        (profession or "").casefold(),
        (char.get("subProfessionId") or "").casefold(),
        (PROFESSION.get(profession) or "").casefold(),
    }


def query_operator_specs(names: list[str] | None = None, role_filter: str = "",
                         owned_only: bool = True) -> dict:
    """离线规格：默认仅查已拥有；owned_only=False 可查看未拥有角色理论机制。

    elite/level/potential 来自 MAA，技能等级/专精/模组未知。顶层费用/阻挡/
    再部署按账号精英阶段取游戏表基础值（不含未知模组）；理论满配集中在 max，
    技能 max 为末级效果，不代表账号已达标。usable/tier_score 保留实际练度语义。
    """
    requested = list(names) if names is not None else None
    if requested is not None and not requested:
        return {"count": 0, "operators": [], "missing": []}
    tables, error = _read_spec_tables()
    if error:
        return {"count": 0, "operators": [], "missing": requested or [], "error": error}
    chars, skills = tables["character_table"], tables["skill_table"]
    index = _playable_by_name(chars)
    missing: list[str] = []
    if requested is None:
        candidates = list(index.values())
    else:
        candidates = []
        for name in requested:
            entry = index.get(name)
            if entry is None:
                missing.append(name)
            else:
                candidates.append(entry)
    operators = []
    attack_ranges = _spec_ranges()
    box_by_name, box_by_id = _box_training()
    for char_id, char in candidates:
        if not _matches_role(char, role_filter):
            continue
        name = char.get("name") or char_id
        box_op = box_by_id.get(char_id) or box_by_name.get(name)
        if owned_only and box_op is None:
            continue
        training = _training(box_op)
        frame = (_operator_frame(char, training["elite"], training["level"])
                 if training["elite"] is not None and training["level"] is not None else {})
        range_id = None
        if training["elite"] is not None:
            for phase in (char.get("phases") or [])[:training["elite"] + 1]:
                range_id = phase.get("rangeId") or range_id
        operators.append({
            "name": name,
            "id": char_id,
            "class": PROFESSION.get(char.get("profession"), char.get("profession")),
            "branch": char.get("subProfessionId"),
            "position": char.get("position"),
            "owned": box_op is not None,
            "actual": {"source": "maa", **training,
                       "skill_level": None, "mastery": None, "module": None},
            "elite": training["elite"],
            "level": training["level"],
            "potential": training["potential"],
            "usable": _is_usable(training["elite"], training["level"]),
            "tier_score": _tier_score(training["elite"], training["level"]),
            "cost": _last_number(frame.get("cost")),
            "block": _last_number(frame.get("blockCnt")),
            "redeploy": _last_number(frame.get("respawnTime")),
            "skills": _skill_specs(char, skills, elite=training["elite"], level=training["level"]),
            "range_offsets": _range_offsets(range_id, attack_ranges),
            "max": {**_promotion_stats(char),
                    "range_offsets": _range_offsets(_range_id(char), attack_ranges)},
        })
    return {"count": len(operators), "operators": operators, "missing": missing}


def rank_operators(role: str | None = None, branch: str | None = None,
                   limit: int = 20, min_elite: int | None = None) -> list[dict]:
    """按实际精英/等级降序返回当前 Box，供上位替代与练度筛选。

    role 接受中文职业名、PROFESSION 枚举或 subProfessionId（复用 _matches_role）；
    branch 精确匹配 subProfessionId（不区分大小写）；limit≤0 返回全部。
    角色表缺失时仍返回 Box 条目，只是 class/branch/position 为 None。
    """
    tables, _ = _read_spec_tables()
    chars = tables.get("character_table") or {}
    by_name = _playable_by_name(chars)
    rows = []
    for op in load_box():
        if op.get("own") is False:
            continue
        name = op.get("name")
        if not name:
            continue
        char_id = op.get("id")
        char = chars.get(char_id) if char_id else None
        if char is None:
            fallback_id, fallback = by_name.get(name, (None, None))
            if fallback is not None:
                char_id, char = fallback_id, fallback
        if role and (char is None or not _matches_role(char, role)):
            continue
        sub = (char or {}).get("subProfessionId")
        if branch and (sub or "").casefold() != branch.strip().casefold():
            continue
        training = _training(op)
        if min_elite is not None and (training["elite"] is None
                                      or training["elite"] < min_elite):
            continue
        rows.append({
            "name": name,
            "id": char_id,
            "class": (PROFESSION.get(char.get("profession"), char.get("profession"))
                      if char else None),
            "branch": sub,
            "position": char.get("position") if char else None,
            "rarity": (char or {}).get("rarity"),
            **training,
            "usable": _is_usable(training["elite"], training["level"]),
            "tier_score": _tier_score(training["elite"], training["level"]),
        })
    rows.sort(key=lambda r: (r["elite"] is None, -(r["elite"] or 0), -(r["level"] or 0),
                             -(r["potential"] or 0), r["name"]))
    return rows if limit is None or limit <= 0 else rows[:limit]


def _owned_names(roster: list[dict], *, override_operators=None) -> set[str]:
    """返回所选 Box 的拥有姓名；未提供 override 时保持全局库统计语义。"""
    own = load_box(override_operators)
    if override_operators is not None or own:
        return {op["name"] for op in own if op.get("own", True)}
    return {op["name"] for op in roster}



def _elite_level_label(elite, level) -> str:
    return f"E{elite}L{level}" if elite is not None and level is not None else "未知"


def _reference_fit(job: dict, box: dict[str, dict]) -> dict:
    """拥有与达标分开；未知技能检查计入分母，但绝不冒充满足或已知低练度。"""
    satisfied = total = 0
    underleveled, unknown = [], []
    for entry in job.get("opers") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        name = entry["name"]
        requirements = entry.get("requirements") or {}
        required = {field: requirements[field] for field in REFERENCE_REQUIREMENTS
                    if requirements.get(field) is not None}
        # 没有技能要求也无法确认作业技能轴可复现，仍需明确记录未知练度。
        training_requirements = {k: v for k, v in required.items() if k != "skill_level"}
        total += (len(training_requirements) or 1) + 1
        op = box.get(name)
        if op is None or op.get("own") is False:
            continue
        training = _training(op)
        unknown.append({"name": name, "field": "skill_level",
                        "required": required.get("skill_level"), "actual": None,
                        "reason": "MAA 未提供技能等级/专精"})
        checks = []
        required_elite = required.get("elite")
        if required_elite is not None:
            checks.append(("elite", None if training["elite"] is None
                           else training["elite"] >= required_elite))
        if required.get("level") is not None:
            higher = (required_elite is not None and training["elite"] is not None
                      and training["elite"] > required_elite)
            checks.append(("level", True if higher else (
                None if training["level"] is None else training["level"] >= required["level"])))
        if not training_requirements:
            checks.append(("eligible", None if None in (training["elite"], training["level"])
                           else _is_usable(training["elite"], training["level"])))
        satisfied += sum(ok is True for _, ok in checks)
        for field, ok in checks:
            if ok is None:
                unknown.append({"name": name, "field": field,
                                "required": required.get(field), "actual": None,
                                "reason": "账号练度字段缺失"})
        failed = [field for field, ok in checks if ok is False]
        if failed:
            underleveled.append({
                "name": name, "required": required or None,
                "actual": {**training, "skill_level": None},
                "reason": (f"实际 {_elite_level_label(training['elite'], training['level'])}，"
                           f"未满足 {','.join(failed)}"
                           + (f"（常用基准 E2 或 E1≥{USABLE_E1_LEVEL}）"
                              if "eligible" in failed else "")),
            })
    return {"fit_score": round(satisfied / total, 3) if total else 1.0,
            "satisfied": satisfied, "total": total, "underleveled": underleveled,
            "unknown": unknown, "complete": not unknown and satisfied == total}


def _cached_references(stage: dict, roster: list[dict], take: int = 3, *, override_operators=None) -> list[dict]:
    """用 stage 的本地作业与所选 roster/override 重新计算拥有、缺失与练度适配。"""
    owned = _owned_names(roster, override_operators=override_operators)
    box = _box_training(override_operators=override_operators)[0]
    results = []
    for path in sorted((DATA / "references" / stage["code"]).glob("*.json"), reverse=True):
        if len(results) >= take:
            break
        job = json.loads(path.read_text(encoding="utf-8"))
        if job.get("stage_name") != stage["stageId"] or not job.get("actions"):
            continue
        opers = [op["name"] for op in job.get("opers", []) if op.get("name")]
        if not opers:
            continue
        results.append({
            "id": int(path.stem) if path.stem.isdigit() else path.stem,
            "uploader": None, "upload_time": None,
            "difficulty": job.get("difficulty"), "title": (job.get("doc") or {}).get("title", ""),
            "opers": opers, "have": [name for name in opers if name in owned],
            "missing": [name for name in opers if name not in owned],
            "fit": _reference_fit(job, box),
            "file": path.relative_to(ROOT).as_posix(), "source": "cache",
        })
    return results


def fetch_references(stage: dict, roster: list[dict], limit: int = 5, take: int = 3,
                     *, override_operators=None) -> list[dict]:
    """下载 stage 参考作业，按 roster/override 计算缺员与练度适配，返回至多 take 条。"""
    out_dir = DATA / "references" / stage["code"]
    out_dir.mkdir(parents=True, exist_ok=True)
    owned = _owned_names(roster, override_operators=override_operators)
    box = _box_training(override_operators=override_operators)[0]
    results = []
    for page in range(1, 7):
        if len(results) >= take:
            break
        url = ("https://prts.maa.plus/copilot/query?level_keyword=" + urllib.parse.quote(stage["stageId"]) +
               f"&page={page}&limit={limit}&order_by=id&desc=true")
        try:
            listing = http_json(url)
        except (OSError, urllib.error.URLError) as exc:
            print(f"[warn] reference query failed: {repr(exc)[:120]}", file=sys.stderr, flush=True)
            return _cached_references(stage, roster, take, override_operators=override_operators)
        payload = listing.get("data") or {}
        for item in payload.get("data", []):
            if len(results) >= take:
                break
            try:
                head = json.loads(item["content"])
                if head.get("stage_name") != stage["stageId"]:
                    continue
                detail = http_json(f"https://prts.maa.plus/copilot/get/{item['id']}")["data"]
                job = json.loads(detail["content"])
            except (OSError, urllib.error.URLError) as exc:
                print(f"[警告] 参考作业 {item['id']} 下载失败：{exc}", file=sys.stderr)
                return _cached_references(stage, roster, take, override_operators=override_operators)
            opers = [o.get("name", "") for o in job.get("opers", []) if o.get("name")]
            if not opers or not job.get("actions"):
                continue
            path = out_dir / f"{item['id']}.json"
            path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
            have = [n for n in opers if n in owned]
            missing = [n for n in opers if n not in owned]
            results.append({
                "id": item["id"], "uploader": item.get("uploader"), "upload_time": item.get("upload_time"),
                "difficulty": job.get("difficulty"), "title": (job.get("doc") or {}).get("title", ""),
                "opers": opers, "have": have, "missing": missing,
                "fit": _reference_fit(job, box),
                "file": path.relative_to(ROOT).as_posix(),
            })
        if not payload.get("has_next"):
            break
    return results or _cached_references(stage, roster, take, override_operators=override_operators)


def _compact_fields(intel: dict) -> dict:
    tiles = intel["tiles"]
    symbols = {"melee": "#", "ranged": "^", "all": "=", "spawn": "S", "goal": "G",
               "teleport": "T", "blocked": "X", "forbidden": "X"}
    cells = {(cell["x"], cell["y"]): cell for cell in tiles["cells"]}
    grid = ["     " + " ".join(f"{x:02d}" for x in range(tiles["width"]))]
    for y in range(tiles["height"]):
        grid.append(f"y={y:02d} " + "  ".join(
            symbols.get(normalized_class(cells[(x, y)]), ".") for x in range(tiles["width"])))
    grid.append("图例：#=地面 ^=高台 ==两用 S=红门 G=蓝门 T=传送 X=阻挡/禁放 .=地板/其他")
    timeline = intel["timeline"]
    waves = list(dict.fromkeys(row["wave"] for row in timeline))
    shown = set(waves[:12])
    lines = [f"t={row['t']:.1f}s W{row['wave']} {row['name']}({row['levelType']}) route={row['route']}"
             for row in timeline if row["wave"] in shown]
    if len(waves) > 12:
        lines.append(f"...共 {len(timeline)} 个出怪动作 / {len(waves)} 波")
    bosses = [f"{boss['name']} hp={boss['hp']} atk={boss['atk']} def={boss['def']} "
              f"res={boss['res']} 扣血={boss.get('lifePointReduce')}" for boss in intel["bosses"]]
    routes = dict.fromkeys((tuple(route["start"]), tuple(route["end"])) for route in intel["routes"])
    return {
        "grid_text": "\n".join(grid), "waves_text": "\n".join(lines),
        "bosses_text": "\n".join(bosses) or "本关无 Boss",
        "routes_text": "\n".join(f"起点({start[0]},{start[1]}) → 终点({end[0]},{end[1]})"
                                  for start, end in routes),
    }


def extract_predefines(level: dict, tables: dict) -> list[dict]:
    """提取预设装置和非部署特殊格；实例 row 向上，地图行序与 MAA 一致。"""
    characters = tables.get("character_table", {})
    map_data = level.get("mapData") or {}
    grid = map_data.get("map") or []
    height = len(grid)
    devices = []
    seen = set()

    def append(key, x, y, source, fields):
        if not key or (key, x, y) in seen:
            return
        if not (type(x) is int and type(y) is int
                and 0 <= y < height and 0 <= x < len(grid[y])):
            raise ValueError(f"预设装置 {key} 坐标越界：({x}, {y})")
        char = characters.get(key, {})
        name = char.get("name") or next((f.get("name") for f in fields if f.get("name")), key)
        desc = next((f.get("description") or f.get("desc") for f in [*fields, char]
                     if f.get("description") or f.get("desc")), None)
        devices.append({"id": key, "name": name, "x": x, "y": y,
                        "source": source, "desc": desc})
        seen.add((key, x, y))

    predefines = level.get("predefines") or {}
    for group in ("tokenInsts", "characterInsts"):
        for item in predefines.get(group) or []:
            inst = item.get("inst", item)
            key = inst["characterKey"]
            char = characters.get(key, {})
            is_trap = char.get("profession") == "TRAP" or key.startswith("trap_")
            is_device = (is_trap or char.get("profession") == "TOKEN"
                         or key.startswith("token_") or item.get("isDevice") or inst.get("isDevice"))
            if group == "characterInsts" and not is_device:
                continue
            pos = item["position"]
            append(key, pos["col"], height - 1 - pos["row"],
                   "trap" if is_trap else "token", [item, inst])

    # 普通地形不算装置；未知的非部署特殊格保留其原始 id/说明，不猜机制名。
    terrain = {"forbidden", "floor", "road", "wall", "start", "end", "hole",
               "empty", "fence_bound", "bigforce"}
    for y, row in enumerate(grid):
        for x, tile_index in enumerate(row):
            tile = map_data["tiles"][tile_index]
            key = tile.get("tileKey")
            if (key and tile.get("buildableType") in ("NONE", 0, None)
                    and key.removeprefix("tile_") not in terrain):
                append(key, x, y, "tile", [tile])
    return devices


def _mechanics_intel(stage: dict, entry: dict, level: dict, devices: list,
                     *, refresh: bool = False) -> tuple[str, dict | None, dict]:
    from src.planner.prts_stage_client import MECHANICS, fetch_stage_page, _mechanic_lines
    from src.planner.doc_fetcher import get_doc_slice

    text = "\n".join((stage["name"], entry.get("description") or "",
                       json.dumps(devices, ensure_ascii=False),
                       json.dumps({key: value for key, value in level.items()
                                   if "mechanic" in key.lower() or key in ("runes", "globalBuffs")},
                                  ensure_ascii=False)))
    hits = [word for word in MECHANICS if word in text]
    if not hits:
        return "", None, {"mechanics_source": {"stage": [], "activity": []},
                          "activity_lookup": None, "warnings": []}
    pieces = []
    source = None
    context = {"mechanics_source": {"stage": hits, "activity": []},
               "activity_lookup": None, "warnings": []}
    try:
        page = fetch_stage_page(stage["code"], refresh=refresh)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"[warn] PRTS 关卡机制读取失败：{exc}", file=sys.stderr)
        context["warnings"].append(f"PRTS 关卡机制读取失败：{exc}")
    else:
        pieces.append(page["stage_mechanics_text"])
        source = {key: page[key] for key in ("source_url", "fetched_at")}
        context.update({key: page[key] for key in ("mechanics_source", "activity_lookup")})
        context["warnings"] = list(dict.fromkeys(page["warnings"]))
        if page.get("activity_text"):
            pieces.insert(0, "活动环境机制（不代表本关全部启用）：\n" + page["activity_text"])
    for word in hits:
        term_title = {"沼泽": "沼泽地段", "芦苇": "芦苇丛"}.get(word, word)
        try:
            term = get_doc_slice(term_title, refresh=refresh)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError,
                json.JSONDecodeError, LookupError) as exc:
            print(f"[warn] PRTS 机制词条 {word} 读取失败：{exc}", file=sys.stderr)
            context["warnings"].append(f"PRTS 机制词条 {term_title} 读取失败：{exc}")
            continue
        snippet = _mechanic_lines(term, limit=300)
        if snippet and snippet not in pieces:
            pieces.append(snippet)
    context["warnings"] = list(dict.fromkeys(context["warnings"]))
    return "\n\n".join(pieces), source, context

def build_compact_intel(stage_code: str, *, refresh: bool = False,
                        override_operators=None, tables=None) -> dict:
    """按需单关情报；剧情不解析地图，教学预设自动下载，白板不查 Wiki。"""
    if tables is None:
        tables = ensure_tables(refresh=refresh)
    try:
        stage = resolve_stage(stage_code, tables, DEFAULT_MAA)
    except ValueError:
        stage = _online_stage(stage_code, tables, DEFAULT_MAA)
    entry = tables["stage_table"]["stages"][stage["stageId"]]
    profile = resolve_stage_profile(stage_code, tables=tables)
    out = stage_intel_path(stage)
    if profile["fixed_squad"]:
        override_operators = profile["predefined_ops"]
    warnings: list[str] = []
    roster = own_roster(tables, override_operators=override_operators, warnings=warnings)
    box_path = DATA / "operators.json"
    sync_time = (json.loads(box_path.read_text(encoding="utf-8")).get("syncTime")
                 if override_operators is None and box_path.exists() else None)
    box = {"owned": len(_owned_names(roster, override_operators=override_operators)),
           "eligible": sum(op["eligible"] for op in roster),
           "source": "maa" if override_operators is None else "override",
           "sync_time": sync_time}
    compact_roster = [{key: op[key] for key in ("id", "name", "elite", "level", "potential",
                      "rarity", "prof", "branch", "position", "eligible")} for op in roster]
    if stage["story_only"]:
        result = {
            "stage": stage, "story_only": True,
            "box": box, "roster": compact_roster,
            "story_hint": "纯剧情节点，无需作战，建议直接跳过", "profile": profile,
            "mechanics_text": "", "wiki_source": None, "predefines_devices": [],
            "predefined_ops": [], "references": [], "kill_total": 0,
            "kill_total_source": "story_only", "grid_text": "", "waves_text": "",
            "bosses_text": "", "routes_text": "", "intel_path": str(out),
            "options": {}, "runes": [], "enemies": {}, "data_version": data_version(),
            "kill_total_check": {"cached": None, "recomputed": 0, "ok": True},
            "warnings": warnings,
        }
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    difficulty = difficulty_group(stage)
    level = None
    if out.exists() and not refresh:
        intel = json.loads(out.read_text(encoding="utf-8"))
        intel["stage"] = stage
        level_path = stage_level_path(stage)
        if level_path.exists():
            level = json.loads(level_path.read_text(encoding="utf-8"))
            intel["tiles"] = parse_tiles(level)
            intel["routes"] = parse_routes(level)
            idx = enemy_index(tables["enemy_database"])
            timeline, bosses = build_timeline(level, idx, difficulty, warnings=warnings)
            intel.update(timeline=timeline, bosses=bosses,
                         enemies=enemy_summary(level, idx, warnings=warnings),
                         options=parse_options(level), runes=parse_runes(level))
            intel["maa_check"] = verify_with_maa(stage, intel["tiles"], DEFAULT_MAA)
        intel["references"] = _cached_references(stage, roster, override_operators=override_operators)
    else:
        level, source = fetch_level(stage)
        tiles = parse_tiles(level)
        idx = enemy_index(tables["enemy_database"])
        timeline, bosses = build_timeline(level, idx, difficulty, warnings=warnings)
        intel = {
            "stage": stage, "source": source, "tiles": tiles,
            "maa_check": verify_with_maa(stage, tiles, DEFAULT_MAA),
            "routes": parse_routes(level), "timeline": timeline, "bosses": bosses,
            "enemies": enemy_summary(level, idx, warnings=warnings),
            "options": parse_options(level), "runes": parse_runes(level),
            "roster": roster,
            "references": _cached_references(stage, roster, override_operators=override_operators)
                          if profile["fixed_squad"] else fetch_references(
                              stage, roster, override_operators=override_operators),
        }
    devices = (extract_predefines(level, tables) if level is not None
               else intel.get("predefines_devices", []))
    mechanics_text, wiki_source, context = _mechanics_intel(stage, entry, level or {}, devices, refresh=refresh)
    intel.update(story_only=False, mechanics_text=mechanics_text, wiki_source=wiki_source,
                 predefines_devices=devices)
    previous_total = intel.get("kill_total")
    context_warnings = list(context.get("warnings") or [])
    intel.update(context)
    intel.update(profile=profile, predefined_ops=profile["predefined_ops"], roster=roster)
    intel["box"] = box
    if box["owned"] < box["eligible"]:
        warnings.append(f"box 与 roster 口径不一致：owned={box['owned']} < eligible={box['eligible']}")
    total = compute_kill_total(intel["timeline"])
    intel.update(kill_total=total, kill_total_source="level_waves")
    intel["kill_total_check"] = cross_check_kill_total(previous_total, total, warnings)
    maa_check = intel.get("maa_check") or {}
    if maa_check.get("mismatches"):
        warnings.append(f"MAA Tile-Pos 交叉校验不一致 {len(maa_check['mismatches'])} 格")
    intel["data_version"] = data_version()
    intel["warnings"] = list(dict.fromkeys([*context_warnings, *warnings]))
    context["warnings"] = intel["warnings"]
    intel.update(_compact_fields(intel))
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(intel, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "stage": {key: stage[key] for key in ("code", "stageId", "name", "stageType", "apCost", "difficulty")},
        "kill_total": total, "kill_total_source": "level_waves",
        **{key: intel[key] for key in ("grid_text", "waves_text", "bosses_text", "routes_text")},
        "profile": profile, "predefined_ops": profile["predefined_ops"], "box": box,
        "roster": compact_roster,
        "story_only": False, "mechanics_text": mechanics_text, "wiki_source": wiki_source,
        "predefines_devices": devices,
        **context,
        "references": [{key: ref[key] for key in ("id", "title", "difficulty", "have", "missing", "fit", "file", "source")
                        if key in ref} for ref in intel["references"]],
        "intel_path": str(out),
    }


def print_summary(stage: dict, tiles: dict, timeline: list, bosses: list, roster: list,
                  refs: list, source: str, routes: list) -> None:
    """CLI 人类可读摘要：只允许 CLI main 调用；库/MCP 路径禁止调用（stdout 是 JSON-RPC）。"""

    def coords(items):
        return ", ".join(f"({c['x']},{c['y']})" for c in items)

    print("\n=== 关卡 ===")
    print(f"输入 {stage['input']} → stageId {stage['stageId']} / levelId {stage['levelId']}")
    print(f"名称 {stage['name']} | {stage['stageType']} | 理智 {stage['apCost']} | 难度 {stage['difficulty']}")
    print(f"地图来源 {source}，{tiles['width']}x{tiles['height']}；格子统计 {tiles['counts']}")

    print("\n=== 可部署格 ===")
    print(f"地面(MELEE) {len(tiles['melee'])}: {coords(tiles['melee'])}")
    print(f"高台/战壕(RANGED) {len(tiles['ranged'])}: {coords(tiles['ranged'])}")
    for cls, items in tiles["special"].items():
        label = {"spawn": "红门/出怪点", "goal": "保护目标/蓝门", "teleport": "传送", "blocked": "不可通行"}[cls]
        print(f"{label} {len(items)}: {coords(items)}")

    uniq = {}
    for r in routes:
        uniq.setdefault((tuple(r["start"]), tuple(r["end"])), []).append(r["index"])
    print("\n=== 路线 ===")
    for (start, end), idxs in uniq.items():
        print(f"route {idxs} : 起点({start[0]},{start[1]}) → 终点({end[0]},{end[1]})")

    print("\n=== 前 10 波出怪 ===")
    wave_order = []
    for row in timeline:
        if row["wave"] not in [w for w, _ in wave_order]:
            wave_order.append((row["wave"], row["t"]))
        if len(wave_order) > 10:
            break
    shown = {w for w, _ in wave_order[:10]}
    for row in [r for r in timeline if r["wave"] in shown]:
        print(f"{row['t']:>7.1f}s  W{row['wave']:<2} {row['name']:<12} {row['levelType']:<7} route={row['route']}")
    if len(wave_order) > 10:
        print(f"... 共 {len(timeline)} 个出怪动作，{len({r['wave'] for r in timeline})} 波")

    print("\n=== Boss ===")
    if bosses:
        for b in bosses:
            print(f"{b['name']} hp={b['hp']} atk={b['atk']} def={b['def']} res={b['res']} 扣血={b['lifePointReduce']}")
    else:
        print("本关无 levelType=BOSS 的敌人")

    print(f"\n=== 我方已拥有干员：{len(roster)} 名（eligible 标记 E2 或 E1≥55） ===")
    for r in roster:
        s = " / ".join(f"{k['n']}技{k['name']}[{k['spType']}/{k['trigger']}]" for k in r["skills"])
        print(f"{r['prof']:<2} {r['name']}({r['elite']}/{r['level']}) {r['position']} {s}"
              + (f" 召唤物{len(r['summons'])}" if r["summons"] else ""))

    print("\n=== 参考作业 ===")
    for ref in refs:
        print(f"#{ref['id']} diff={ref['difficulty']} {ref['title']} by {ref['uploader']}")
        print(f"  拥有({len(ref['have'])}/{len(ref['opers'])}): {'、'.join(ref['have']) or '无'}")
        print(f"  缺失: {'、'.join(ref['missing']) or '无'}")
        print(f"  {ref['file']}")
    if not refs:
        print("未取到有效作业")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="获取关卡格子、波次、Boss、干员与参考作业情报")
    ap.add_argument("stage", help="关卡代号（TL-EX-8）或 stageId（act55side_ex08）")
    ap.add_argument("--maa-dir", default=str(DEFAULT_MAA))
    ap.add_argument("--refresh", action="store_true", help="重新下载缓存的游戏数据表")
    ap.add_argument("--refs", type=int, default=3, help="保存的参考作业数量")
    args = ap.parse_args(argv)
    try:
        return _fetch_stage_intel(args)
    except (OSError, RuntimeError, ValueError, urllib.error.URLError) as exc:
        print(f"[error] 关卡情报获取失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def _fetch_stage_intel(args: argparse.Namespace) -> int:
    tables = ensure_tables(refresh=args.refresh)
    stage = resolve_stage(args.stage, tables, Path(args.maa_dir))
    level, source = fetch_level(stage)

    tiles = parse_tiles(level)
    idx = enemy_index(tables["enemy_database"])
    warnings: list[str] = []
    timeline, bosses = build_timeline(level, idx, difficulty_group(stage), warnings=warnings)
    routes = parse_routes(level)
    check = verify_with_maa(stage, tiles, Path(args.maa_dir))
    roster = own_roster(tables, warnings=warnings)
    refs = fetch_references(stage, roster, take=args.refs)

    total = compute_kill_total(timeline)
    intel = {
        "stage": stage,
        "source": source,
        "tiles": tiles,
        "maa_check": check,
        "routes": routes,
        "timeline": timeline,
        "kill_total": total,
        "kill_total_source": "level_waves",
        "bosses": bosses,
        "enemies": enemy_summary(level, idx, warnings=warnings),
        "options": parse_options(level),
        "runes": parse_runes(level),
        "roster": roster,
        "references": refs,
        "data_version": data_version(),
        "kill_total_check": cross_check_kill_total(None, total, warnings),
        "warnings": list(dict.fromkeys(warnings)),
    }
    out = stage_intel_path(stage)
    out.write_text(json.dumps(intel, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"敌人总数：{intel['kill_total']}（level_waves）")
    print_summary(stage, tiles, timeline, bosses, roster, refs, source, routes)
    if check is None:
        print("\n[校验] 未找到 MAA Tile-Pos 数据，跳过交叉核对")
    elif check["mismatches"]:
        print(f"\n[校验] 与 MAA Tile-Pos 不一致 {len(check['mismatches'])} 格: {check['mismatches'][:8]}")
    else:
        print(f"\n[校验] 与 MAA Tile-Pos 一致 {check['checked']}/{check['checked']} 格")
    print(f"\n情报已写入 {out.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
