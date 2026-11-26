"""分层情报查询：默认简报、按 section 展开、原始回退与 PRTS 敌人视觉。

简报只做索引；分段查询返回原始结构并附
raw_path / source_path / warnings；格式化失败显式写入 warnings，不静默吞掉。
敌人立绘/头像按需从 PRTS 图鉴页获取并缓存到 data/doc_cache/enemy_visual/，
找不到或只有相似页面时返回 ok=False，不用相似图片冒充。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.planner.doc_fetcher import DOC_CACHE, http_get, search_prts
from src.planner.intel import (DATA, build_compact_intel, ensure_tables,
                               stage_level_path)

PRTS_API = "https://prts.wiki/api.php"
PRTS_WIKI = "https://prts.wiki/w/"
VISUAL_CACHE = DOC_CACHE / "enemy_visual"
TTL_DAYS = 7
MECHANICS_SUMMARY_CHARS = 400
SECTIONS = ("map", "routes", "timeline", "enemies", "mechanics", "roster",
            "references", "raw")
# section → intel 产物必须具备的字段；raw 永远可用（只回路径）。
SECTION_FIELDS = {
    "map": ("tiles", "options"),
    "routes": ("routes",),
    "timeline": ("timeline",),
    "enemies": ("enemies",),
    "mechanics": ("runes", "predefines_devices", "mechanics_text"),
    "roster": ("roster",),
    "references": ("references",),
    "raw": (),
}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


# ------------------------------------------------------------------ 通用小工具
def _query_hint(stage_code: str) -> str:
    return ('简报只是索引：用 query_stage_intel("%s", section="enemies") 展开；'
            '单条敌人用 key="enemy_x@1" 或敌人名；'
            '图鉴用 get_enemy_visual("敌人名")' % stage_code)


def _warnings(*groups) -> list[str]:
    merged: list[str] = []
    for group in groups:
        merged.extend(group or [])
    return list(dict.fromkeys(merged))


def _norm_name(name: str) -> str:
    """图鉴名归一：忽略空白与引号差异，用于严格同名判断。"""
    return re.sub(r"[\s\u3000\"'“”‘’]", "", name or "").casefold()


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _fresh(record: dict) -> bool:
    try:
        fetched = datetime.fromisoformat(record["fetched_at"])
    except (KeyError, ValueError):
        return False
    return datetime.now(timezone.utc) < fetched + timedelta(days=record.get("ttl_days", TTL_DAYS))


def _source_path(stage: dict, intel: dict) -> str | None:
    """关卡原始 JSON 路径；本地没有产物时退回上游 URL。"""
    level = stage_level_path(stage or {})
    if level.exists():
        return str(level)
    return intel.get("source")


def _available_sections(intel: dict) -> list[str]:
    return [section for section in SECTIONS
            if all(field in intel for field in SECTION_FIELDS[section])]


def _artifact(stage_code: str) -> tuple[dict, dict, Path]:
    """调用现有 build_compact_intel，再读它落盘的完整 v2 情报。"""
    meta = build_compact_intel(stage_code)
    if "intel_path" not in meta:
        raise ValueError(f"build_compact_intel({stage_code!r}) 未返回 intel_path")
    raw_path = Path(meta["intel_path"])
    if not raw_path.is_file():
        raise FileNotFoundError(f"情报文件不存在：{raw_path}（stage={stage_code}）")
    intel = json.loads(raw_path.read_text(encoding="utf-8"))
    return meta, intel, raw_path


# --------------------------------------------------------------- 关卡简报
def get_stage_brief(stage_code: str) -> dict:
    """关卡索引简报：不含完整 timeline / roster / 地图格 / 长机制文本。"""
    meta, intel, raw_path = _artifact(stage_code)
    return _brief(meta, intel, raw_path)


def _brief(meta: dict, intel: dict, raw_path: Path) -> dict:
    stage = intel.get("stage") or meta.get("stage") or {}
    story_only = bool(intel.get("story_only") or meta.get("story_only"))
    warnings = _warnings(intel.get("warnings"), meta.get("warnings"))
    source_path = _source_path(stage, intel)

    def safe(label: str, builder):
        try:
            return builder()
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
            warnings.append(f"简报 {label} 格式化失败：{exc}；raw_path={raw_path}")
            return None

    def build_options():
        return dict(intel.get("options") or {})

    def build_map():
        if "tiles" not in intel:
            if story_only:
                return None
            raise KeyError("tiles")
        tiles = intel["tiles"]
        return {
            "width": tiles["width"], "height": tiles["height"],
            "cells": tiles["width"] * tiles["height"],
            "counts": dict(tiles.get("counts") or {}),
            "melee": len(tiles.get("melee") or []),
            "ranged": len(tiles.get("ranged") or []),
        }

    def build_routes():
        if "routes" not in intel:
            if story_only:
                return {"count": 0, "pairs": []}
            raise KeyError("routes")
        rows = intel["routes"]
        pairs: dict[tuple, list] = {}
        for route in rows:
            pair = (tuple(route["start"]), tuple(route["end"]))
            pairs.setdefault(pair, []).append(route["index"])
        return {
            "count": len(rows),
            "pairs": [{"start": list(start), "end": list(end), "routes": indexes}
                      for (start, end), indexes in pairs.items()],
        }

    def build_bosses():
        if "bosses" not in intel:
            if story_only:
                return []
            raise KeyError("bosses")
        fields = ("key", "name", "levelType", "hp", "atk", "def", "res",
                  "lifePointReduce")
        return [{field: boss.get(field) for field in fields} for boss in intel["bosses"]]

    def build_enemies():
        if "enemies" not in intel:
            if story_only:
                return []
            raise KeyError("enemies")
        return [{"key": label, "name": entry.get("name")}
                for label, entry in intel["enemies"].items()]

    def build_mechanics():
        text = intel.get("mechanics_text") or ""
        return {
            "summary": text[:MECHANICS_SUMMARY_CHARS],
            "truncated": len(text) > MECHANICS_SUMMARY_CHARS,
            "sources": dict(intel.get("mechanics_source") or {}),
            "wiki_source": intel.get("wiki_source"),
        }

    def build_references():
        rows = intel["references"] if "references" in intel else (meta.get("references") or [])
        return [{
            "id": ref.get("id"), "title": ref.get("title"),
            "ops": len(ref.get("opers") or []),
            "have": len(ref.get("have") or []),
            "missing": list(ref.get("missing") or []),
        } for ref in rows]

    brief = {
        "stage": stage,
        "story_only": story_only,
        "kill_total": intel.get("kill_total", meta.get("kill_total")),
        "options": safe("options", build_options),
        "map": safe("map", build_map),
        "routes": safe("routes", build_routes),
        "bosses": safe("bosses", build_bosses),
        "enemies": safe("enemies", build_enemies),
        "mechanics": safe("mechanics", build_mechanics),
        "box": intel.get("box") or meta.get("box") or {},
        "references": safe("references", build_references),
        "available_sections": _available_sections(intel),
        "query_hint": _query_hint(stage.get("code") or (meta.get("stage") or {}).get("code") or "关卡代号"),
        "warnings": _warnings(warnings),
        "raw_path": str(raw_path),
        "source_path": source_path,
    }
    if story_only:
        brief["story_hint"] = meta.get("story_hint") or intel.get("story_hint")
    return brief


# --------------------------------------------------------------- 分段查询
def query_stage_intel(stage_code: str, section: str,
                      key: str | None = None) -> dict:
    """按 section 展开情报；key 仅用于 enemies 单条与 references 按 ID。"""
    section = (section or "").strip().lower()
    if section not in SECTIONS:
        raise ValueError(f"未知 section {section!r}；可选 {list(SECTIONS)}")
    if key is not None and section not in ("enemies", "references"):
        raise ValueError(f"section={section} 不接受 key；key 仅用于 enemies/references")

    meta, intel, raw_path = _artifact(stage_code)
    stage = intel.get("stage") or meta.get("stage") or {}
    stage_code = stage.get("code") or stage_code
    warnings = _warnings(intel.get("warnings"), meta.get("warnings"))
    missing = [field for field in SECTION_FIELDS[section] if field not in intel]
    if missing:
        raise LookupError(
            f"{stage_code} 缺少 {section} 数据（缺字段 {missing}）；"
            f"raw_path={raw_path}；available_sections={_available_sections(intel)}")

    result = {
        "stage": stage, "section": section, "key": key,
        "raw_path": str(raw_path), "source_path": _source_path(stage, intel),
        "warnings": warnings,
    }

    if section == "map":
        data = {"tiles": intel["tiles"], "options": intel["options"]}
    elif section == "routes":
        data = intel["routes"]
    elif section == "timeline":
        data = intel["timeline"]
    elif section == "enemies":
        if key is None:
            data = intel["enemies"]
        else:
            label, entry, matched_by = _find_enemy(intel["enemies"], str(key))
            result.update(key=label, matched_label=label, matched_by=matched_by)
            data = entry
    elif section == "mechanics":
        data = {"runes": intel["runes"],
                "predefines_devices": intel["predefines_devices"],
                "mechanics_text": intel["mechanics_text"]}
    elif section == "roster":
        data = intel["roster"]
    elif section == "references":
        if key is None:
            data = intel["references"]
        else:
            data = _find_reference(intel["references"], key)
    else:  # raw：只回路径，不倾倒整份内容
        level = stage_level_path(stage)
        data = {
            "intel_path": str(raw_path),
            "level_path": str(level) if level.exists() else None,
            "intel_source": intel.get("source"),
        }
    result["data"] = data
    return result


def _find_enemy(enemies: dict, key: str) -> tuple[str, dict, str]:
    """依次按 label（key@level）、基础 key、敌人名精确匹配；歧义即 KeyError。"""
    if key in enemies:
        return key, enemies[key], "label"
    by_key = [(label, entry) for label, entry in enemies.items()
              if (entry or {}).get("key") == key]
    if len(by_key) == 1:
        return by_key[0][0], by_key[0][1], "key"
    if len(by_key) > 1:
        raise KeyError(f"敌人 key {key!r} 有多个等级变体：{[label for label, _ in by_key]}；"
                       "请用 key@level 查询")
    needle = _norm_name(key)
    by_name = [(label, entry) for label, entry in enemies.items()
               if _norm_name((entry or {}).get("name") or "") == needle]
    if len(by_name) == 1:
        return by_name[0][0], by_name[0][1], "name"
    if len(by_name) > 1:
        raise KeyError(f"敌人名 {key!r} 对应多个等级变体：{[label for label, _ in by_name]}；"
                       "请用 key@level 查询")
    raise KeyError(f"敌人 {key!r} 未命中；可用 key 形如 enemy_xxx 或 enemy_xxx@1，"
                   "或用 section='enemies' 查看全部目录")


def _find_reference(references: list, key) -> dict:
    text = str(key).strip()
    for ref in references:
        if str(ref.get("id")) == text:
            return ref
    raise KeyError(f"参考作业 {key!r} 未命中；可用 ID：{[ref.get('id') for ref in references]}")


# ------------------------------------------------------------ PRTS 敌人视觉
def _prts_json(params: dict) -> dict:
    url = PRTS_API + "?" + urllib.parse.urlencode(
        {**params, "format": "json", "formatversion": 2})
    response = http_get(url)
    payload = json.loads(response["body"].decode("utf-8"))
    if "error" in payload:
        raise LookupError(f"PRTS 查询失败：{payload['error']}")
    return payload


def _file_kind(file_title: str) -> str | None:
    name = (file_title or "").split(":", 1)[-1]
    if "头像" in name:
        return "avatar"
    if "图标" in name:
        return "icon"
    if "立绘" in name or name.casefold().startswith(("avg ", "avg_")):
        return "portrait"
    if "npc" in name.casefold():
        return "portrait"
    return None


def parse_enemy_page(payload: dict) -> dict:
    """解析 action=query&prop=images|pageimages 的响应。"""
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages:
        raise LookupError("PRTS 响应缺少 pages")
    page = pages[0]
    title = page.get("title")
    if page.get("missing") is not None or page.get("invalid") is not None \
            or page.get("pageid") in (None, -1):
        return {"title": title, "missing": True, "images": [],
                "avatar_file": None, "portrait_file": None, "pageimage": None}
    images = [image.get("title", "") for image in page.get("images") or []]
    avatar = next((title for title in images if _file_kind(title) == "avatar"), None)
    portrait = next((title for title in images if _file_kind(title) == "portrait"), None)
    return {"title": title, "missing": False, "images": images,
            "avatar_file": avatar, "portrait_file": portrait,
            "pageimage": page.get("pageimage")}


def parse_imageinfo(payload: dict) -> str | None:
    pages = (payload.get("query") or {}).get("pages") or []
    if not pages:
        raise LookupError("PRTS 响应缺少 pages")
    info = pages[0].get("imageinfo") or []
    return info[0].get("url") if info else None


def pick_enemy_image(page: dict) -> tuple[str | None, str | None]:
    """优先立绘，其次头像；没有可确认图片时返回 (None, None)。"""
    if page.get("portrait_file"):
        return page["portrait_file"], "portrait"
    if page.get("avatar_file"):
        return page["avatar_file"], "avatar"
    return None, None


def match_enemy_title(query: str, titles: list[str]) -> str | None:
    """严格同名匹配（忽略空白/引号）；多个同名或只有相似名返回 None/报错。"""
    needle = _norm_name(query)
    exact = [title for title in titles if _norm_name(title) == needle and "/" not in title]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise LookupError(f"PRTS 多个页面与 {query!r} 同名：{exact}；拒绝猜测")
    return None


def enemy_records(db: dict) -> list[dict]:
    """敌人表 → 去重 [{key, name}]；同 key 多等级取首个非空名。"""
    records: dict[str, dict] = {}
    for entry in db.get("enemies") or []:
        key = entry.get("Key")
        if not key:
            continue
        name = next((_unwrap((variant.get("enemyData") or {}).get("name"))
                     for variant in entry.get("Value") or []
                     if _unwrap((variant.get("enemyData") or {}).get("name"))), None)
        records.setdefault(key, {"key": key, "name": name})
    return list(records.values())


def _unwrap(node):
    if isinstance(node, dict) and "m_value" in node:
        return node["m_value"]
    return node


def _local_enemy_records() -> list[dict]:
    path = DATA / "gamedata_cache" / "enemy_database.json"
    if not path.is_file():
        return []
    return enemy_records(json.loads(path.read_text(encoding="utf-8")))


def _resolve_enemy_query(enemy: str, records: list[dict]) -> tuple[str | None, str, list[str]]:
    """输入 key 或名称 → (matched_key, 图鉴页名, warnings)。"""
    base = enemy.split("@", 1)[0].strip()
    if re.match(r"^(enemy|token|trap)_", base):
        record = next((row for row in records if row["key"] == base), None)
        if record is None:
            return None, "", [f"本地敌人表未收录 key={base}，无法解析图鉴名"]
        if not record.get("name"):
            return None, "", [f"本地敌人表 key={base} 没有名称字段"]
        return base, record["name"], []
    needle = _norm_name(enemy)
    hits = [row for row in records if _norm_name(row.get("name") or "") == needle]
    if len(hits) == 1:
        return hits[0]["key"], hits[0]["name"], []
    if len(hits) > 1:
        keys = sorted({row["key"] for row in hits})
        return None, enemy, [f"名称 {enemy!r} 对应多个敌人 key：{keys}；matched_key 置空"]
    return None, enemy, []


def _fetch_page(name: str) -> dict:
    page = parse_enemy_page(_prts_json({
        "action": "query", "titles": name, "redirects": 1,
        "prop": "images|pageimages", "pithumbsize": "400"}))
    if page["missing"]:
        titles = search_prts(name, limit=8)
        exact = match_enemy_title(name, titles)
        if exact is None:
            raise LookupError(f"PRTS 未找到与 {name!r} 完全匹配的敌人图鉴页；候选：{titles}")
        page = parse_enemy_page(_prts_json({
            "action": "query", "titles": exact, "redirects": 1,
            "prop": "images|pageimages", "pithumbsize": "400"}))
    if page["missing"]:
        raise LookupError(f"PRTS 敌人图鉴页不存在：{name!r}")
    if _norm_name(page["title"]) != _norm_name(name):
        raise LookupError(f"PRTS 图鉴页 {page['title']!r} 与 {name!r} 不完全一致；"
                          "拒绝用相似敌人页面代替")
    return page


def _image_url(file_title: str) -> str:
    url = parse_imageinfo(_prts_json({
        "action": "query", "titles": file_title, "prop": "imageinfo",
        "iiprop": "url"}))
    if not url:
        raise LookupError(f"PRTS 文件没有图片 URL：{file_title}")
    return url


def _optional_image_url(file_title: str, warnings: list[str]) -> str | None:
    if not file_title:
        return None
    try:
        return _image_url(file_title)
    except (LookupError, urllib.error.HTTPError, urllib.error.URLError,
            TimeoutError, json.JSONDecodeError) as exc:
        warnings.append(f"PRTS 附加图片 {file_title} 获取失败：{exc}")
        return None


def _download_image(url: str) -> bytes:
    body = http_get(url)["body"]
    if not body:
        raise LookupError(f"PRTS 图片为空：{url}")
    return body


def _slug(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]',
                  lambda match: urllib.parse.quote(match[0], safe=""), name)


def _visual_failure(enemy: str, matched_key: str | None,
                    warnings: list[str], error: str) -> dict:
    return {
        "ok": False, "name": None, "query": enemy, "matched_key": matched_key,
        "page_url": None, "image_url": None, "image_kind": None,
        "avatar_url": None, "portrait_url": None, "image_path": None,
        "source": None, "error": error, "warnings": _warnings(warnings),
    }


def get_enemy_visual(enemy: str, refresh: bool = False) -> dict:
    """按需获取 PRTS 敌人图鉴页与立绘/头像，缓存图片到 doc_cache/enemy_visual/。"""
    enemy = (enemy or "").strip()
    if not enemy:
        return _visual_failure(enemy, None, [], "敌人 key 或名称为空")

    records = _local_enemy_records()
    matched_key, page_name, warnings = _resolve_enemy_query(enemy, records)
    if not records:
        warnings.append("本地敌人表缺失，matched_key 无法确定")
    if not page_name:
        return _visual_failure(enemy, matched_key, warnings,
                               f"无法确定 {enemy!r} 的图鉴页名；{warnings[0]}")

    meta_path = VISUAL_CACHE / (_slug(page_name) + ".json")
    cached = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() else None
    if cached and not refresh and _fresh(cached):
        image_path = Path(cached.get("image_path") or "")
        if cached.get("ok") and image_path.is_file():
            # 图片缓存与查询无关；matched_key 必须按本次输入重算，避免旧歧义结果污染。
            return {**cached, "query": enemy,
                    "matched_key": matched_key if matched_key is not None
                    else cached.get("matched_key"),
                    "warnings": _warnings(cached.get("warnings"), warnings),
                    "cached": True}

    try:
        page = _fetch_page(page_name)
        file_title, kind = pick_enemy_image(page)
        if not file_title:
            raise LookupError(f"PRTS 图鉴页 {page['title']!r} 没有可确认的头像/立绘图片；"
                              "不返回相似图片")
        image_url = _image_url(file_title)
        body = _download_image(image_url)
    except (LookupError, urllib.error.HTTPError, urllib.error.URLError,
            TimeoutError, json.JSONDecodeError) as exc:
        error = f"PRTS 敌人视觉获取失败：{exc}"
        warnings.append(error)
        return _visual_failure(enemy, matched_key, warnings, error)

    avatar_url = image_url if kind == "avatar" else _optional_image_url(
        page.get("avatar_file"), warnings)
    portrait_url = image_url if kind == "portrait" else _optional_image_url(
        page.get("portrait_file"), warnings)

    suffix = Path(urllib.parse.urlsplit(image_url).path).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ".png"
    image_path = VISUAL_CACHE / (_slug(page_name) + suffix)
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(body)

    record = {
        "ok": True, "name": page["title"], "query": enemy,
        "matched_key": matched_key,
        "page_url": PRTS_WIKI + urllib.parse.quote(page["title"], safe=""),
        "file_title": file_title, "image_kind": kind,
        "image_url": image_url, "avatar_url": avatar_url,
        "portrait_url": portrait_url, "image_path": str(image_path),
        "source": f"prts.wiki 敌人图鉴（{file_title}）",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "ttl_days": TTL_DAYS, "warnings": _warnings(warnings),
    }
    _write_json(meta_path, record)
    return {**record, "cached": False}


# --------------------------------------------------------------- 下一关解析
STAGE_TABLE = DATA / "gamedata_cache" / "stage_table.json"
RAID_SUFFIX = "#f#"
# completeState 只出现 PASS / COMPLETE（见 stage_table 全量统计）；其余取值按未知处理。
PASSED_STATES = {"PASS", "COMPLETE", "PASSED", "CLEARED", "DONE"}
UNPASSED_STATES = {"NONE", "FAIL", "FAILED", "NOT_PASSED", "UNPLAYED"}


def _failure(error: str, warnings=None, current=None) -> dict:
    return {"ok": False, "error": error, "current": current, "next": None,
            "remaining": [], "unlock_condition": [], "blocked_by": [],
            "warnings": _warnings(warnings, [error])}


def _load_stage_table(table=None, table_path=None) -> tuple[dict | None, str | None]:
    """返回 (stages 映射, 错误)。显式传入的表优先，其次本地缓存，最后联网同步。"""
    if table is not None:
        stages = table.get("stages") if isinstance(table, dict) else None
        if isinstance(table, dict) and not isinstance(stages, dict):
            stages = table if all(isinstance(v, dict) for v in table.values()) else None
        if not isinstance(stages, dict):
            return None, "关卡表结构异常：缺少 stages 映射"
        return stages, None
    path = Path(table_path) if table_path else STAGE_TABLE
    if not path.is_file():
        try:
            stages = ensure_tables()["stage_table"]["stages"]
        except Exception as exc:  # noqa: BLE001 - 网络/远端表失败都要变成明确的 error
            return None, (f"关卡表缺失且无法获取：{path}"
                          f"（{type(exc).__name__}: {exc}）")
        return stages, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"stage_table 读取失败：{path}（{type(exc).__name__}: {exc}）"
    stages = payload.get("stages") if isinstance(payload, dict) else None
    if not isinstance(stages, dict):
        return None, f"stage_table 结构异常：缺少 stages 映射（{path}）"
    return stages, None


def _story_only(stage: dict) -> bool:
    return bool(stage.get("isStoryOnly") if "isStoryOnly" in stage
                else stage.get("is_story_only"))


def _chain_node(stage: dict) -> bool:
    """顺序节点：普通难度、非突袭 #f#；剧情关保留为前置节点。"""
    return (str(stage.get("difficulty") or "NORMAL").upper() == "NORMAL"
            and not str(stage.get("stageId") or "").endswith(RAID_SUFFIX))


def _playable(stage: dict) -> bool:
    """下一关候选：普通难度、非剧情、突袭 #f# 不算独立关卡。"""
    return _chain_node(stage) and not _story_only(stage)


def _find_stage(stages: dict, stage_code: str) -> tuple[dict | None, list[str]]:
    """代号/stageId → 关卡；突袭输入按普通关解析，同代号多条时取 stageId 最小者。"""
    warnings: list[str] = []
    text = str(stage_code or "").strip()
    if not text:
        return None, ["关卡代号为空"]
    if text.endswith(RAID_SUFFIX):
        text = text[: -len(RAID_SUFFIX)]
        warnings.append(f"{stage_code} 是突袭关：突袭不作为独立下一关，按普通关 {text} 解析")
    direct = stages.get(text) or stages.get(text.lower())
    if isinstance(direct, dict) and str(direct.get("difficulty") or "").upper() == "NORMAL":
        return direct, warnings
    matches: list[dict] = []
    for keep in (_playable, _chain_node):
        matches = sorted((stage for stage in stages.values()
                          if str(stage.get("code") or "").upper() == text.upper()
                          and keep(stage)),
                         key=lambda stage: str(stage.get("stageId") or ""))
        if matches:
            break
    if not matches:
        return None, warnings
    if len(matches) > 1:
        warnings.append(f"代号 {text} 命中 {len(matches)} 个普通关，取 stageId 最小者 "
                        f"{matches[0]['stageId']}")
    return matches[0], warnings


def _zone_order(stages: dict, zone: str) -> tuple[list[str], list[str], list[str]]:
    """zone 内(stageId, 剧情关)拓扑序 → (全节点序, 可玩关序, warnings)。

    顺序由 unlockCondition 决定：FC-4 放出 FC-TR-2，FC-TR-2 再放出 FC-5；
    剧情关不输出但保留为顺序节点，否则 FC-8 会因为没有前置而飘到最前面。
    同级按 stageId 排序，保证确定性。
    """
    warnings: list[str] = []
    nodes = sorted(stage_id for stage_id, stage in stages.items()
                   if str(stage.get("zoneId") or "") == zone
                   and _chain_node(stage))
    node_set = set(nodes)
    prereqs: dict[str, set[str]] = {}
    for stage_id in nodes:
        stage = stages[stage_id]
        prereqs[stage_id] = {str(cond.get("stageId") or "")
                             for cond in stage.get("unlockCondition") or []
                             if str(cond.get("stageId") or "") in node_set
                             and str(cond.get("stageId") or "") != stage_id}
    dependents: dict[str, set[str]] = {stage_id: set() for stage_id in nodes}
    for stage_id, required in prereqs.items():
        for required_id in required:
            dependents[required_id].add(stage_id)
    pending = {stage_id: len(prereqs[stage_id]) for stage_id in nodes}
    ready = sorted(stage_id for stage_id, count in pending.items() if count == 0)
    order: list[str] = []
    while ready:
        stage_id = ready.pop(0)
        order.append(stage_id)
        for dependent in sorted(dependents[stage_id]):
            pending[dependent] -= 1
            if pending[dependent] == 0:
                ready.append(dependent)
        ready.sort()
    if len(order) != len(nodes):
        leftovers = sorted(node_set - set(order))
        warnings.append("zone 内解锁条件存在环，剩余关卡退回 stageId 顺序：" +
                        ", ".join(leftovers[:5]))
        order.extend(leftovers)
    playable = [stage_id for stage_id in order if not _story_only(stages[stage_id])]
    return order, playable, warnings


def _pass_state(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().upper()
        if text in PASSED_STATES:
            return True
        if text in UNPASSED_STATES:
            return False
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    return None


def _state_for(progress: dict | None, stage_id: str) -> bool | None:
    """有进度表时缺项 = 未通关；完全没有进度信息时才返回未知。"""
    if progress is None:
        return None
    return _pass_state(progress[stage_id]) if stage_id in progress else False


def _progress_map(stages: dict, progress) -> dict | None:
    """进度 → {stageId: 值}；None 表示完全没有进度信息（一切都未知）。"""
    if progress is None:
        return None
    items = progress.items() if isinstance(progress, dict) else ((key, True) for key in progress)
    normalised: dict[str, object] = {}
    for key, value in items:
        text = str(key or "").strip().rstrip(RAID_SUFFIX).strip()
        if not text:
            continue
        if isinstance(stages.get(text), dict):
            normalised[text] = value
            continue
        for stage in stages.values():
            if str(stage.get("code") or "").upper() == text.upper() and _playable(stage):
                normalised[str(stage.get("stageId"))] = value
                break
    return normalised


def _stage_ref(stage: dict) -> dict:
    return {"code": stage.get("code"), "stageId": stage.get("stageId"),
            "name": stage.get("name"), "zoneId": stage.get("zoneId")}


def _unlock_rows(stages: dict, stage: dict, progress: dict | None) -> tuple[list[dict], list[str]]:
    """解锁条件逐条判定：已满足 True / 未满足 False / 进度未知 None。"""
    rows: list[dict] = []
    for cond in stage.get("unlockCondition") or []:
        stage_id = str(cond.get("stageId") or "")
        state = _state_for(progress, stage_id)
        if state is None:
            label = "unknown"
        else:
            label = "passed" if state else "not_passed"
        rows.append({"stageId": stage_id,
                     "code": (stages.get(stage_id) or {}).get("code"),
                     "completeState": cond.get("completeState"),
                     "satisfied": state, "state": label})
    blocked = [row["stageId"] for row in rows if row["satisfied"] is not True]
    return rows, blocked


def resolve_next_stage(stage_code: str, progress=None, *, table=None,
                       table_path=None) -> dict:
    """解析同一 zone 内按解锁链排出的下一关；不猜进度。

    - 通关（PASS/COMPLETE）时给下一关，否则回当前关；`#f#` 突袭永不作为独立关卡。
    - `progress` 支持 {stageId|code: "PASS"/"COMPLETE"/bool} 或集合；不传则一切未知，
      解锁条件标记 satisfied=None 并写进 blocked_by，绝不假定「已通关」。
    - `table` 可注入 {"stages": {...}}（测试用），否则读 STAGE_TABLE / 缺失时联网同步。
    返回 {ok, current, next, remaining, unlock_condition, blocked_by, warnings}；
    缺表、未知代号、zone 内没有后续关卡时 ok=False 并给出 error。
    """
    warnings: list[str] = []
    stages, error = _load_stage_table(table, table_path)
    if error:
        return _failure(error)
    current, lookup_warnings = _find_stage(stages, stage_code)
    warnings.extend(lookup_warnings)
    if current is None:
        return _failure(f"关卡表中没有代号 {stage_code!r}：需要 stage.code（如 FC-4）"
                        "或 stageId（如 act22side_04）", warnings)

    current_id = str(current.get("stageId") or "")
    zone = str(current.get("zoneId") or "")
    order, playable, order_warnings = _zone_order(stages, zone)
    warnings.extend(order_warnings)
    if current_id not in order:
        return _failure(f"关卡 {current.get('code')} 不在 zone {zone!r} 的可解析序列里"
                        "（突袭/非普通难度关卡不作为解析起点）", warnings)
    playable_set = set(playable)
    progress_states = _progress_map(stages, progress)
    if progress_states is None:
        cleared = None
        warnings.append("未提供通关进度：不假定任何关卡已通关，解锁条件一律标记为未知")
    else:
        cleared = _state_for(progress_states, current_id)

    advance = bool(cleared)
    if _story_only(current):
        advance = True
        warnings.append(f"{current.get('code')} 是剧情关，不参与战斗；next 取其后的可玩关卡")

    position = order.index(current_id)
    next_id = current_id
    if advance:
        next_id = next((stage_id for stage_id in order[position + 1:]
                        if stage_id in playable_set), None)
        if next_id is None:
            return _failure(f"{current.get('code')} 已是 zone {zone!r} 内最后一个非剧情关卡，"
                            "本函数不跨 zone 解析下一关", warnings,
                            {**_stage_ref(current), "cleared": cleared})

    next_stage = stages[next_id]
    rows, blocked = _unlock_rows(stages, next_stage, progress_states)
    if blocked:
        warnings.append(f"下一关 {next_stage.get('code')} 的前置未满足或未知："
                        + ", ".join(blocked))
    successors = sorted(stage_id for stage_id in playable_set
                        if any(str(cond.get("stageId") or "") == current_id
                               for cond in stages[stage_id].get("unlockCondition") or []))
    if len(successors) > 1:
        warnings.append("当前关有 %d 个后续分支（%s），按 stageId 顺序取 %s"
                        % (len(successors), ", ".join(successors),
                           next_stage.get("stageId")))

    next_position = order.index(next_id)
    remaining = [_stage_ref(stages[stage_id])
                 for stage_id in order[next_position + 1:] if stage_id in playable_set]
    return {"ok": True, "current": {**_stage_ref(current), "cleared": cleared},
            "next": _stage_ref(next_stage), "remaining": remaining,
            "unlock_condition": rows, "blocked_by": blocked,
            "warnings": _warnings(warnings)}
