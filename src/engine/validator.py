#!/usr/bin/env python3
"""Generic copilot validator: deployment tiles and static attack coverage.

Every Deploy must land on a tile type that matches the operator position:
MELEE operators on `melee` cells, RANGED operators on `ranged` cells.
The same walk replays the actions as a deployment timeline: Deploy occupies one
slot and Retreat frees it again, so a job that deploys more operators than the
level allows (`options.characterLimit`) is rejected, and a same-operator
re-deploy that follows its own retreat too closely is flagged as impossible.
Damage-oriented ranged operators must cover a spawned enemy route with their
highest-promotion base range. Ground routes merge four-neighbor waypoint paths;
flying, empty-checkpoint and unreachable segments use lines marked approx=True.

Usage:
  python cli.py validate <stage_code> <squad_json>
  python -m src.engine.validator <stage_code> <squad_json>

The intel file is resolved as data/levels/<stage_code>-intel.json; nothing in
this module is tied to a specific stage.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
import urllib.error
import urllib.request
from collections import deque

from src.planner.intel import load_box, parse_routes, stage_cache_key, tile_positions

ROOT = Path(__file__).resolve().parents[2]
CHARS = ROOT / "data" / "gamedata_cache" / "character_table.json"
RANGES = CHARS.with_name("range_table.json")
RANGE_URL = ("https://raw.githubusercontent.com/Kengxxiao/ArknightsGameData/"
             "master/zh_CN/gamedata/excel/range_table.json")

# 中文 type 与 MAA 英文 type 等价（copilot schema 明确「中英文皆可」）。
_ACTION_ALIASES = {"部署": "Deploy", "撤退": "Retreat"}

# 这几类动作的 name 明确指向一个单位；SpeedUp/SkillDaemon 无名字，Retreat 不校验名单。
_OPERATOR_TARGET_ACTIONS = {"Deploy", "BulletTime", "Skill", "SkillUsage", "SetUnitLocation"}
# character_table 里的非可玩实体：装置/召唤物，允许不出现在 opers 中。
_SYSTEM_PROFESSIONS = {"TRAP", "TOKEN"}

# 再部署节奏窗口。依据：character_table 里 respawnTime 普遍是 70 秒，撤退后必须等
# 完整的再部署 CD；而 copilot 动作不带时间戳，唯一可离线读取的时间代理是 kills 条件
# （每次等待都会推进绝对击杀数）。因此用「间隔动作数 + 击杀推进」双阈值：
#   间隔 0 个动作且击杀 0 推进 = 物理上不可能，判 error；
#   间隔 < 3 个动作且击杀 0 推进 = 正常换防（补位）通常至少会穿插技能/他人部署/等
#   待击杀，判 warning 提醒人工确认。两个阈值都取保守下限，宁可少报不误杀。
REDEPLOY_MIN_GAP_ACTIONS = 3
REDEPLOY_MIN_KILLS = 1


def _action_type(action: dict):
    """归一化动作类型：中文别名折叠成 Deploy/Retreat，其余原样返回。"""
    kind = action.get("type")
    return _ACTION_ALIASES.get(kind, kind) if isinstance(kind, str) else kind


def _deploy_limit(intel: dict, intel_file: Path) -> tuple[int | None, str | None]:
    """返回 (可同时部署干员上限, 来源标签)，取不到时返回 (None, None)。

    字段查实结论：关卡情报本身没有部署上限字段，stage_table 的 maxSlot 是队伍槽位
    而不是部署上限；真正的上限在关卡数据（Activities/.../level_xxx.json）的
    options.characterLimit（TL-EX-8=8、TR-1=10、FC-TR-1=3）。情报与关卡数据同目录
    同源命名（<code>-intel.json 与 <code>.json），所以先认情报里人工补写的
    max_deploy_count，再回落到关卡数据的 options.characterLimit。
    """
    for holder, label in ((intel, "intel.max_deploy_count"),
                          (intel.get("profile") or {}, "intel.profile.max_deploy_count")):
        value = holder.get("max_deploy_count")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value, label
    level_path = intel_file.with_name(f"{stage_cache_key(intel['stage'])}.json")
    if level_path.is_file():
        level = json.loads(level_path.read_text(encoding="utf-8"))
        value = (level.get("options") or {}).get("characterLimit")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value, "level.options.characterLimit"
    return None, None


def load_attack_ranges() -> dict[str, set[tuple[int, int]]]:
    """读取基础射程；仅缓存缺失时下载一次，网络或缓存失败直接报错。"""
    if RANGES.exists():
        table = json.loads(RANGES.read_text(encoding="utf-8"))
    else:
        request = urllib.request.Request(RANGE_URL, headers={"User-Agent": "arknights-auto/1.0"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
            table = json.loads(payload.decode("utf-8"))
            RANGES.parent.mkdir(parents=True, exist_ok=True)
            RANGES.write_bytes(payload)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"射程表下载失败 {RANGE_URL}: {exc}") from exc
    # 游戏 row 轴向上；转换成 MAA 向下的 dy，再在屏幕坐标系旋转。
    return {key: {(grid["col"], -grid["row"]) for grid in entry["grids"]}
            for key, entry in table.items()}


def _route_paths(intel: dict) -> tuple[set[tuple[int, int]], bool]:
    active_routes = {row["route"] for row in intel.get("timeline", []) if row.get("route") is not None}
    flying_routes = {row["route"] for row in intel.get("timeline", [])
                     if row.get("levelType", "").upper() in {"FLY", "FLYING", "AIR"}}
    # 芦苇丛/沼泽等 buildableType=NONE 的 LOWLAND 仍可走，不能把 class=forbidden 当墙。
    walkable = {(cell["x"], cell["y"]) for cell in intel.get("tiles", {}).get("cells", [])
                if cell.get("heightType") == "LOWLAND"
                and cell["tileKey"] not in {"forbidden", "wall", "hole", "empty", "fence_bound",
                                            "bigforce", "telin", "telout"}}
    approx = False
    paths = {}
    for route in intel.get("routes", []):
        if route["index"] not in active_routes:
            continue
        cells = paths.setdefault(route["index"], set())
        checkpoints = (route.get("checkpoints", []) if route.get("motion_mode", "WALK") == "WALK"
                       and route["index"] not in flying_routes else [])
        points = [route["start"], *checkpoints, route["end"]]
        for start, end in zip(points, points[1:]):
            start, end = tuple(start), tuple(end)
            use_bfs = (walkable and route.get("motion_mode", "WALK") == "WALK"
                       and route.get("checkpoints") and route["index"] not in flying_routes)
            connected = False
            if use_bfs:
                # shortcut: 四邻接最短路、同距优先纵轴；不模拟游戏斜行、动态障碍或传送。
                queue = deque([start])
                parents = {start: None}
                while queue and end not in parents:
                    x, y = queue.popleft()
                    dx = 1 if end[0] > x else -1
                    dy = 1 if end[1] > y else -1
                    for neighbor in ((x, y + dy), (x + dx, y), (x, y - dy), (x - dx, y)):
                        if neighbor in walkable and neighbor not in parents:
                            parents[neighbor] = (x, y)
                            queue.append(neighbor)
                if end in parents:
                    connected = True
                    current = end
                    while current is not None:
                        cells.add(current)
                        current = parents[current]
            if not connected:
                approx = True
                x0, y0 = start
                x1, y1 = end
                steps = max(abs(x1 - x0), abs(y1 - y0))
                for step in range(steps + 1):
                    fraction = step / steps if steps else 0
                    cells.add((round(x0 + (x1 - x0) * fraction), round(y0 + (y1 - y0) * fraction)))
    return set().union(*paths.values()), approx


def _path_cells(intel: dict) -> set[tuple[int, int]]:
    return _route_paths(intel)[0]


def _coverage_counts(offsets: set[tuple[int, int]], x: int, y: int,
                     path_cells: set[tuple[int, int]]) -> dict[str, int]:
    counts = {}
    for direction in ("RIGHT", "DOWN", "LEFT", "UP"):
        counts[direction] = len({(x + dx, y + dy) for dx, dy in offsets} & path_cells)
        offsets = {(-dy, dx) for dx, dy in offsets}
    return counts


def operator_positions(*, override_operators=None) -> dict[str, str]:
    """返回所选 Box 的姓名到 MELEE/RANGED 映射；无 id 时按缓存表姓名反查。"""
    chars = json.loads(CHARS.read_text(encoding="utf-8"))
    by_name = {char["name"]: char for char in chars.values() if char.get("name")}
    out = {}
    for op in load_box(override_operators):
        char = chars.get(op.get("id")) or by_name.get(op["name"])
        if char and char.get("position") in {"MELEE", "RANGED"}:
            out[op["name"]] = char["position"]
    return out


def default_intel_path(stage: str) -> Path:
    return ROOT / "data" / "levels" / f"{stage}-intel.json"


def validate_job(job_or_path, *, stage_code: str | None = None,
                 intel_path: str | None = None, override_operators=None) -> dict:
    """按 stage/intel 离线校验作业；override 替代 Box，返回逐项错误及非预设未知干员告警。"""
    result = {"ok": False, "checked": 0, "errors": [], "warnings": [],
              "stage_name": None, "summary": "", "rows": [], "input_error": False}

    def issue(index, name, location, want, actual, message):
        return {"action_index": index, "operator": name, "location": location,
                "want": want, "actual": actual, "message": message}

    try:
        if isinstance(job_or_path, dict):
            job = job_or_path
        elif isinstance(job_or_path, str) and job_or_path.lstrip().startswith("{"):
            job = json.loads(job_or_path)
        else:
            job = json.loads(Path(job_or_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        result["input_error"] = True
        result["summary"] = f"作业读取失败：{exc}"
        result["errors"].append(issue(-1, "", None, None, None, result["summary"]))
        return result
    result["stage_name"] = job.get("stage_name")
    result["difficulty"] = job.get("difficulty")
    if intel_path is not None:
        path = Path(intel_path)
    else:
        identifier = stage_code or job.get("stage_name") or ""
        path = default_intel_path(identifier)
        if not path.exists():
            for candidate in sorted((ROOT / "data" / "levels").glob("*-intel.json")):
                cached = json.loads(candidate.read_text(encoding="utf-8"))
                if cached["stage"]["stageId"] == identifier:
                    path = candidate
                    break
    result["intel_path"] = str(path)
    if not path.exists():
        result["input_error"] = True
        result["summary"] = f"情报文件缺失：{path}，请先生成关卡情报"
        result["errors"].append(issue(-1, "", None, None, None, result["summary"]))
        return result
    intel = json.loads(path.read_text(encoding="utf-8"))
    if any("checkpoints" not in route for route in intel.get("routes", [])):
        level_path = path.with_name(f"{stage_cache_key(intel['stage'])}.json")
        if level_path.is_file():
            intel["routes"] = parse_routes(json.loads(level_path.read_text(encoding="utf-8")))
    result["intel_stage_id"] = intel["stage"]["stageId"]
    if job.get("stage_name") != intel["stage"]["stageId"]:
        result["errors"].append(issue(
            -1, "", None, intel["stage"]["stageId"], job.get("stage_name"),
            f"stage_name {job.get('stage_name')} 与情报 stageId {intel['stage']['stageId']} 不一致"))
    cells = {(cell["x"], cell["y"]): cell for cell in intel["tiles"]["cells"]}
    fixed_squad = (intel.get("profile") or {}).get("family") == "tr_fixed"
    predefined_ops = (intel.get("profile") or {}).get("predefined_ops", intel.get("predefined_ops", []))
    predefined_names = {op["name"] for op in predefined_ops} if fixed_squad else set()
    system_names = predefined_names | {
        device.get("name") for device in intel.get("predefines_devices", []) if device.get("name")}
    if fixed_squad and override_operators is None:
        override_operators = predefined_ops
    positions = operator_positions(override_operators=override_operators)
    path_cells, approx = _route_paths(intel)
    result["path_cells"] = len(path_cells)
    result["approx"] = approx
    characters = json.loads(CHARS.read_text(encoding="utf-8"))
    by_name = {char["name"]: char for char in characters.values() if char.get("name")}
    selected_characters = {op["name"]: characters.get(op.get("id")) or by_name.get(op.get("name"))
                           for op in load_box(override_operators)}
    # 作业名单硬校验：动作里点名的单位必须是 opers/groups 里的候选，或可证明的系统实体。
    declared_squad = {op.get("name") for op in job.get("opers", []) if op.get("name")}
    for group in job.get("groups", []):
        if group.get("name"):
            declared_squad.add(group["name"])
        declared_squad.update(op.get("name") for op in group.get("opers", []) if op.get("name"))
    reported_missing: set[str] = set()
    for index, action in enumerate(job.get("actions", [])):
        if _action_type(action) not in _OPERATOR_TARGET_ACTIONS:
            continue
        name = action.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        if name in declared_squad or name in system_names or name in reported_missing:
            continue
        char = selected_characters.get(name) or by_name.get(name)
        if char is not None and char.get("profession") in _SYSTEM_PROFESSIONS:
            continue
        reported_missing.add(name)
        if char is not None or name in selected_characters:
            result["errors"].append(issue(
                index, name, action.get("location"), "opers/groups", None,
                f"#{index} {name}: 干员不在 opers/groups 候选名单中，作业部署名单与 Box 不一致"))
        else:
            result["errors"].append(issue(
                index, name, action.get("location"), "system-predefine", None,
                f"#{index} {name}: 无法证明是系统装置/召唤物"
                "（不在 character_table/Box/关卡预设中），禁止开打"))
    attack_ranges = None
    for index, action in enumerate(job.get("actions", [])):
        location = action.get("location")
        if (_action_type(action) != "Deploy" or not isinstance(location, (list, tuple))
                or len(location) != 2 or any(value is None or value == "" for value in location)):
            continue
        name = action.get("name") or ""
        x, y = location
        location = [x, y]
        result["checked"] += 1
        prefix = f"#{index} {name} ({x},{y}): "
        position = positions.get(name)
        need = {"MELEE": "melee", "RANGED": "ranged"}.get(position)
        system_entity = name in system_names or (
            (selected_characters.get(name) or by_name.get(name) or {}).get("profession")
            in _SYSTEM_PROFESSIONS)
        unknown = name not in positions and not system_entity
        if unknown:
            result["warnings"].append(issue(index, name, location, None, None,
                                             prefix + "未知干员（不在当前 Box 中），跳过站位类型校验"))
        in_bounds = (type(x) is int and type(y) is int
                     and 0 <= x < intel["tiles"]["width"] and 0 <= y < intel["tiles"]["height"])
        cell = cells.get((x, y)) if in_bounds else None
        actual = f"{cell['class']}/{cell['tileKey']}" if cell else "越界"
        accepted = tile_positions(cell) if cell else set()
        ok = cell is not None and (name not in positions or need in accepted)
        status = "OK" if ok else "BAD"
        if ok and unknown:
            status = "WARN"
        result["rows"].append({"action_index": index, "operator": name, "position": position or "?",
                               "location": location, "actual": actual, "status": status})
        if cell is None:
            result["errors"].append(issue(index, name, location, need, None, prefix + "坐标越界"))
        elif name in positions and not ok:
            result["errors"].append(issue(index, name, location, need, actual,
                                         prefix + f"需要 {need}，实际 {actual}"))
        char = selected_characters.get(name)
        if (ok and position == "RANGED" and char is not None and path_cells
                and char.get("profession") not in {"PIONEER", "MEDIC", "SUPPORT"}):
            range_id = next((phase["rangeId"] for phase in reversed(char["phases"])
                             if phase.get("rangeId")), None)
            if range_id is None:
                raise ValueError(f"干员 {name} 缺少基础射程 rangeId")
            if attack_ranges is None:
                attack_ranges = load_attack_ranges()
            counts = _coverage_counts(attack_ranges[range_id], x, y, path_cells)
            result["rows"][-1]["coverage_counts"] = counts
            direction = (action.get("direction") or "RIGHT").upper()
            if direction not in counts:
                raise ValueError(f"干员 {name} 朝向无效：{direction}")
            if counts[direction] == 0:
                recommended = max(counts, key=counts.get)
                result["rows"][-1]["status"] = "BAD"
                result["errors"].append(issue(
                    index, name, location, "coverage", "0",
                    f"#{index} {name} ({x},{y}) 朝向 {direction} 与敌方路径 0 交集，建议朝向 {recommended}"
                    + ("（含近似路径 approx=True）" if approx else "")))
    # 时间轴回放：Deploy 占一个部署位、Retreat 释放一个；同一干员撤退后立刻再部署不可能。
    squad = set(declared_squad) | predefined_names | set(positions)
    limit, limit_source = _deploy_limit(intel, path)
    result["max_deploy_count"] = limit
    result["max_deploy_source"] = limit_source
    if limit is None:
        result["warnings"].append(issue(
            -1, "", None, None, None,
            f"部署上限未知：{path.name} 与同目录关卡数据都没有 options.characterLimit，跳过部署上限校验"))
    live = peak = kills_seen = 0
    retreats = {}
    for index, action in enumerate(job.get("actions", [])):
        kills = action.get("kills")
        if isinstance(kills, int) and not isinstance(kills, bool):
            kills_seen = max(kills_seen, kills)  # kills 是绝对条件，回放里取推进到最远的那个。
        kind = _action_type(action)
        name = action.get("name") or ""
        if name not in squad:
            # 装置/召唤物（如「自爆榴弹」）也写 type=Deploy 占格子，但不吃部署位。
            continue
        if kind == "Deploy":
            live += 1
            peak = max(peak, live)
            if limit is not None and live > limit:
                result["errors"].append(issue(
                    index, name, action.get("location"), limit, live,
                    f"#{index} {name}: 部署数 {live} 超过上限 {limit}（来源 {limit_source}）"))
            retreat = retreats.pop(name, None)
            if retreat is not None:
                gap = index - retreat["index"] - 1
                kill_span = kills_seen - retreat["kills"]
                if gap == 0 and kill_span < REDEPLOY_MIN_KILLS:
                    result["errors"].append(issue(
                        index, name, action.get("location"), retreat["index"], gap,
                        f"#{index} {name}: 撤退 #{retreat['index']} 后未隔任何动作立即再部署，"
                        "再部署 CD 不可能转好"))
                elif gap < REDEPLOY_MIN_GAP_ACTIONS and kill_span < REDEPLOY_MIN_KILLS:
                    result["warnings"].append(issue(
                        index, name, action.get("location"),
                        f"gap>={REDEPLOY_MIN_GAP_ACTIONS}", f"gap={gap}, kills+{kill_span}",
                        f"#{index} {name}: 撤退 #{retreat['index']} 后仅隔 {gap} 个动作、"
                        f"击杀 +{kill_span} 就再部署，疑似再部署 CD 冲突"))
        elif kind == "Retreat":
            live = max(0, live - 1)
            retreats[name] = {"index": index, "kills": kills_seen}
    result["deploy_peak"] = peak
    result["ok"] = not result["errors"]
    if result["ok"]:
        result["summary"] = f"校验通过：{result['checked']} 个部署位，{len(result['warnings'])} 条告警"
    else:
        result["summary"] = f"校验失败：{len(result['errors'])} 个错误，禁止开打"
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="校验自动作业部署格子")
    ap.add_argument("stage", help="关卡代号（TL-EX-8）或 stageId（act55side_ex08）")
    ap.add_argument("job", help="作业 JSON 路径")
    ap.add_argument("--intel", default=None, help="指定情报 JSON 路径")
    args = ap.parse_args(argv)
    result = validate_job(args.job, stage_code=args.stage, intel_path=args.intel)
    if result["input_error"]:
        print(result["summary"], file=sys.stderr)
        return 2
    print(f"作业: {Path(args.job).name}  stage={result['stage_name']}  difficulty={result['difficulty']}")
    print(f"情报: {Path(result['intel_path']).name}  stageId={result['intel_stage_id']}")
    for row in result["rows"]:
        x, y = row["location"]
        print(f"  {row['status']:<4} #{row['action_index']} {row['operator']:<6} "
              f"{row['position']:<6} ({x},{y}) -> {row['actual']}")
    for warning in result["warnings"]:
        print("  WARN", warning["message"])
    if not result["ok"]:
        print("\n校验失败，禁止开打:")
        for error in result["errors"]:
            print("  -", error["message"])
        return 1
    print(f"\n{result['summary']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
