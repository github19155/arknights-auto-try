"""按需读取 PRTS 单关页面；七天缓存，不预热整站数据。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import sys
import urllib.error
import urllib.parse

from src.planner.doc_fetcher import http_get, clean_wikitext, search_prts, fetch_prts_page

CACHE = Path(__file__).resolve().parents[2] / "data" / "cache_ondemand" / "wiki_stages"
MECHANICS = ("沼泽", "芦苇", "迷彩", "灼燃", "火焰", "蔓延", "熄灭", "传送", "冰冻", "酸液")
TTL = timedelta(days=7)


def _story_text(text: str) -> bool:
    if re.search(r"\|\s*战斗关卡\s*=\s*false\b", text, re.I):
        return True
    if any(word in text for word in ("纯剧情", "无战斗", "剧情关")):
        return True
    return not any(word in text for word in ("敌人数量", "部署上限", "敌方情报", "地图大小"))


def _mechanic_lines(text: str, limit: int = 800) -> str:
    lines = clean_wikitext(text).splitlines()
    selected = set()
    for index, line in enumerate(lines):
        if any(word in line for word in MECHANICS):
            selected.update(range(max(0, index - 1), min(len(lines), index + 2)))
    return "\n".join(dict.fromkeys(lines[index].strip() for index in sorted(selected)
                                    if lines[index].strip()))[:limit]


def _stage_slice(raw: str) -> str:
    # 先限制为机制字段/章节，再取命中行上下文，避免带入关卡元数据和剧情。
    fields = re.findall(r"^\|\s*(?:关卡描述|关卡特殊规则|特殊规则|环境调控装置)\s*=([^\n]*)", raw, re.M)
    sections = re.findall(r"^==\s*(?:环境调控装置|关卡特殊规则|特殊规则)\s*==\s*\n(.*?)(?=^==[^=]|\Z)", raw, re.M | re.S)
    selected = _mechanic_lines("\n".join(fields + sections))
    if selected:
        return selected
    description = re.search(r"^\|\s*关卡描述\s*=([^\n]*)", raw, re.M)
    return ((clean_wikitext(description.group(1))[:300] + "\n") if description else "") + "[mechanics: 未命中专用机制词，仅保留关卡描述]"


def _activity_slice(text: str) -> str:
    lines = clean_wikitext(text).splitlines()
    selected = set()
    for index, line in enumerate(lines):
        if any(word in line for word in MECHANICS):
            selected.update(range(index, min(len(lines), index + 16)))
    return "\n".join(lines[index] for index in sorted(selected))[:1200]


def _activity_context(raw: str, warnings: list[str]) -> tuple[dict | None, str, dict | None]:
    activity = re.search(r"\|\s*活动名称\s*=\s*([^|\n}]+)", raw)
    stage_id = re.search(r"\|\s*关卡id\s*=\s*(act[^_\s|}]+)_", raw, re.I)
    lookup = None
    title = activity.group(1).strip() if activity else ""
    if not title and stage_id:
        prefix = stage_id.group(1)
        for path in sorted(CACHE.glob("*.json")):
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("stage_id", "").startswith(prefix + "_") and cached.get("activity_page"):
                title = cached["activity_page"]["name"]
                lookup = {"via": "sibling_cache", "page": cached["title"], "fetched_at": cached["fetched_at"]}
                break
        if not title:
            try:
                titles = search_prts(prefix, limit=6)
                own_id = re.search(r"\|\s*关卡id\s*=\s*([^|\n}]+)", raw, re.I).group(1).strip().casefold()
                candidate = next((name for name in titles if prefix.casefold() in name.casefold()
                                  and name.casefold().replace(" ", "_") != own_id), None)
                lookup = {"via": "search", "page": candidate or "", "fetched_at": datetime.now(timezone.utc).isoformat()}
                if candidate:
                    sibling = fetch_prts_page(candidate)
                    activity = re.search(r"\|\s*活动名称\s*=\s*([^|\n}]+)", sibling["wikitext"])
                    title = activity.group(1).strip() if activity else ""
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, LookupError) as exc:
                warnings.append(f"活动归属 {prefix} 查询失败：{exc}")
            if not title:
                warnings.append(f"未从 {prefix} 的搜索页面取得活动名称，仅保留本关机制")
    if not title:
        return None, "", lookup
    try:
        try:
            page = fetch_prts_page(title)
        except LookupError:
            titles = search_prts(title)
            if not titles:
                warnings.append(f"活动页面与搜索均未命中：{title}")
                return None, "", lookup
            page = fetch_prts_page(titles[0])
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, LookupError) as exc:
        warnings.append(f"活动机制 {title} 读取失败：{exc}")
        return None, "", lookup
    return ({"name": title, "source_url": "https://prts.wiki/w/" + urllib.parse.quote(page["title"], safe="")},
            _activity_slice(page["wikitext"]), lookup)


def fetch_stage_page(stage_code: str, refresh: bool = False) -> dict:
    """关卡代号优先，缺页时搜索；网络故障允许读取已有的过期副本。"""
    code = stage_code.strip().upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9_-]*", code):
        raise ValueError(f"Invalid stage code: {stage_code!r}")
    path = CACHE / f"{code}.json"
    cached = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    now = datetime.now(timezone.utc)
    if (cached and "stage_mechanics_text" in cached and not refresh
            and now - datetime.fromisoformat(cached["fetched_at"]) < TTL):
        return cached
    title = code
    try:
        for attempt in range(2):
            url = "https://prts.wiki/api.php?" + urllib.parse.urlencode({
                "action": "parse", "page": title, "prop": "wikitext", "format": "json",
                "formatversion": 2, "redirects": 1,
            })
            response = http_get(url, etag=cached.get("etag") if cached and "stage_mechanics_text" in cached else None)
            if response["status"] == 304 and cached:
                cached["fetched_at"] = now.isoformat()
                path.write_text(json.dumps(cached, ensure_ascii=False, indent=2), encoding="utf-8")
                return cached
            payload = json.loads(response["body"].decode("utf-8"))
            if "error" not in payload:
                break
            error = payload["error"]
            if error["code"] != "missingtitle" or attempt:
                raise ValueError(f"PRTS page {title!r}: {error}")
            titles = search_prts(code)
            if not titles:
                raise ValueError(f"PRTS stage not found: {code}; attempted {url}")
            title = titles[0]
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        if cached is None:
            raise
        print(f"[warn] PRTS {code} 更新失败，读取过期缓存：{exc}", file=sys.stderr)
        return cached
    page = payload["parse"]
    raw = page["wikitext"]
    clean = clean_wikitext(raw)
    raid = re.search(r"^==\s*突袭[^\n]*==\s*\n(.*?)(?=^==[^=]|\Z)", raw, re.M | re.S)
    stage_id = re.search(r"\|\s*关卡id\s*=\s*([^\n|}]+)", raw, re.I)
    stage_name = re.search(r"\|\s*关卡名\s*=\s*([^\n|}]+)", raw)
    warnings = []
    stage_mechanics = [word for word in MECHANICS if word in raw or word in clean]
    activity_page, activity_text, activity_lookup = (_activity_context(raw, warnings)
        if stage_mechanics else (None, "", None))
    activity_mechanics = [word for word in MECHANICS if word in activity_text]
    result = {
        "stage_code": code, "title": page.get("title", title),
        "source_url": "https://prts.wiki/w/" + urllib.parse.quote(page.get("title", title), safe=""),
        "fetched_at": now.isoformat(), "etag": response.get("etag"),
        "stage_mechanics_text": _stage_slice(raw),
        "clean_text": clean, "summary": re.sub(r"\s+", " ", clean)[:300],
        "mechanics": list(dict.fromkeys(stage_mechanics + activity_mechanics)),
        "mechanics_source": {"stage": stage_mechanics, "activity": activity_mechanics},
        "activity_page": activity_page, "activity_text": activity_text, "warnings": warnings,
        "activity_lookup": activity_lookup,
        "last_modified": response.get("last_modified"), "ttl_days": 7,
        "bytes": len(response["body"]), "status": response["status"],
        "raid_rules": clean_wikitext(raid.group(1)) if raid else "",
        "story_only": _story_text(raw),
        "stage_id": stage_id.group(1).strip() if stage_id else "",
        "stage_name": stage_name.group(1).strip() if stage_name else "",
    }
    CACHE.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def is_story_only(stage_code: str) -> bool:
    return fetch_stage_page(stage_code)["story_only"]
