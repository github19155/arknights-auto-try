#!/usr/bin/env python3
"""MAA 原生特殊玩法任务桥：参数生成 + 任务提交 + 原生事件摘要。

本模块不连接设备、不启动任务、不点屏，只做三件事：

1. ``build_*_params`` 按 MAA 集成文档生成任务参数 JSON；
2. ``run_*`` 用调用方注入的 ``asst``（已由调用方 load/connect）调用 ``append_task`` 提交；
3. 给定 ``events_path`` 时复用 ``recorder.parse_maa_events`` 产出事件摘要。

字段名与结构对照 MAA 集成文档
（https://docs.maa.plus/zh-cn/protocol/integration.html）以及本机 MaaCore 实际解析：
``Roguelike``（theme/mode/squad/roles/core_char_list/starts_count/...）、
``SSSCopilot``（filename + loop_times）、
``ParadoxCopilot``（filename 与 list 二选一；
``is_paradox`` 自 v6.1.2 起弃用，悖论模拟一律走 ParadoxCopilot 任务）。

核心会静默忽略未知参数键，因此本模块对未知键、已移除模式、主题与模式不匹配
一律 Fail-Fast 抛出，避免拼写错误变成"看起来在跑其实没生效"。
"""
from __future__ import annotations

import json
from pathlib import Path

from src.engine.recorder import parse_maa_events

MAA_TASK_TYPES = {
    "roguelike": "Roguelike",
    "sss": "SSSCopilot",
    "paradox": "ParadoxCopilot",
}

# 资源目录 resource/roguelike/<theme>/ 与核心主题枚举一致。
ROGUELIKE_THEMES = ("Phantom", "Mizuki", "Sami", "Sarkaz", "JieGarden", "BlackFlow")
# mode -> 支持该模式的主题；0/1/4/6/7 全主题通用。
ROGUELIKE_MODE_THEMES = {
    0: ROGUELIKE_THEMES,
    1: ROGUELIKE_THEMES,
    4: ROGUELIKE_THEMES,
    6: ROGUELIKE_THEMES,
    7: ROGUELIKE_THEMES,
    5: ("Sami",),
    10001: ("Sarkaz",),
    20001: ("JieGarden",),
    30001: ("BlackFlow",),
    30003: ("BlackFlow",),
}
ROGUELIKE_MODES = tuple(sorted(ROGUELIKE_MODE_THEMES))
ROGUELIKE_REMOVED_MODES = {
    2: "原「兼顾」模式已移除，核心会拒绝",
    3: "模式 3 未开放，核心会拒绝",
}

# stop_when 的短名与原生键；一次可给多个停止条件。
STOP_WHEN_ALIASES = {
    "final_boss": "stop_at_final_boss",
    "stop_at_final_boss": "stop_at_final_boss",
    "max_level": "stop_at_max_level",
    "stop_at_max_level": "stop_at_max_level",
    "investment_full": "stop_when_investment_full",
    "stop_when_investment_full": "stop_when_investment_full",
}

BLACKFLOW_STRATEGIES = (
    "baby_animal", "investment", "burn_with_investment", "burn",
    "ending1", "ending2", "ending3",
)
BLACKFLOW_CULTIVATION_TARGETS = (
    "swaddled_cat", "swaddled_feathered_serpent", "swaddled_dog", "swaddled_cerberus",
)
BLACKFLOW_ANIMALS = ("swaddled_cat", "swaddled_dog", "swaddled_cerberus", "swaddled_feathered_serpent")
PLAYTIME_TARGETS = (1, 2, 3)

# 集成文档 Roguelike 全部字段；theme/mode/squad/operators/stop_when 之外的键走 **maa_kw。
ROGUELIKE_PARAM_KEYS = frozenset({
    "enable", "theme", "mode", "squad", "roles", "core_char", "core_char_list",
    "use_support", "use_nonfriend_support", "starts_count", "difficulty",
    "stop_at_final_boss", "stop_at_max_level", "investment_enabled", "investments_count",
    "stop_when_investment_full", "investment_with_more_score", "start_with_elite_two",
    "only_start_with_elite_two", "refresh_trader_with_dice", "first_floor_foldartal",
    "start_foldartal_list", "collectible_mode_start_list", "use_foldartal",
    "check_collapsal_paradigms", "double_check_collapsal_paradigms",
    "expected_collapsal_paradigms", "monthly_squad_auto_iterate", "monthly_squad_check_comms",
    "deep_exploration_auto_iterate", "collectible_mode_shopping", "collectible_mode_squad",
    "start_with_seed", "blackflow_strategy", "blackflow_cultivation_target",
    "blackflow_start_animals", "blackflow_start_rewards", "blackflow_start_utopia",
    "blackflow_start_reward_priority", "find_playTime_target",
})
# 由命名参数表达，禁止在 **maa_kw 里重复传。
_ROGUELIKE_NAMED_KEYS = frozenset({
    "theme", "squad", "core_char", "core_char_list",
    "stop_at_final_boss", "stop_at_max_level", "stop_when_investment_full",
})
SSS_PARAM_KEYS = frozenset({"enable", "loop_times"})
PARADOX_PARAM_KEYS = frozenset({"enable"})


def _require_int(name: str, value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数：{value!r}")
    return value


def _require_str_list(name: str, value) -> list[str]:
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} 必须是字符串数组：{value!r}")
    return list(value)


def _resolve_file(path, name: str) -> str:
    if not isinstance(path, (str, Path)) or not str(path):
        raise ValueError(f"{name} 必须是作业 JSON 文件路径：{path!r}")
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"{name} 作业文件不存在：{resolved}")
    return str(resolved)


def _check_kw(kw: dict, allowed, task_type: str, reserved=frozenset()) -> None:
    dup = sorted(set(kw) & reserved)
    if dup:
        raise ValueError(f"{task_type} 参数 {', '.join(dup)} 必须用命名参数传入，不要放进 **maa_kw")
    unknown = sorted(set(kw) - allowed)
    if unknown:
        raise ValueError(
            f"未知的 {task_type} 参数：{', '.join(unknown)}；"
            f"MAA 会静默忽略拼写错误，故在此拦截。可用键：{', '.join(sorted(allowed))}"
        )


def _finalize(params: dict) -> dict:
    """关键契约：产出必须是可直接交给 MaaCore 的 JSON 对象。"""
    json.dumps(params)
    return params


def _core_char_list(operators) -> list[dict]:
    if isinstance(operators, (str, bytes)) or not hasattr(operators, "__iter__"):
        raise ValueError(f"operators 必须是干员名或 {{'name','use_support'}} 的序列：{operators!r}")
    entries: list[dict] = []
    for item in operators:
        if isinstance(item, str):
            name, use_support = item, False
        elif isinstance(item, dict):
            unknown = set(item) - {"name", "use_support"}
            if unknown:
                raise ValueError(f"operators 条目含未知字段 {sorted(unknown)}：{item!r}")
            name = item.get("name")
            use_support = item.get("use_support", False)
            if not isinstance(use_support, bool):
                raise ValueError(f"use_support 必须是布尔值：{item!r}")
        else:
            raise ValueError(f"operators 条目必须是字符串或对象：{item!r}")
        if not isinstance(name, str) or not name:
            raise ValueError(f"operators 条目缺少干员名：{item!r}")
        entries.append({"name": name, "use_support": use_support})
    if not entries:
        raise ValueError("operators 不能为空序列；不指定开局干员时传 None")
    if len(entries) > 3:
        raise ValueError(f"core_char_list 最多 3 项（对应开局三次招募）：{len(entries)}")
    names = [entry["name"] for entry in entries]
    if len(set(names)) != len(names):
        raise ValueError(f"core_char_list 干员不能重复：{names}")
    return entries


def _stop_flags(stop_when) -> dict:
    if stop_when is None:
        return {}
    if isinstance(stop_when, str):
        stop_when = [stop_when]
    flags: dict = {}
    for item in stop_when:
        key = STOP_WHEN_ALIASES.get(item)
        if key is None:
            raise ValueError(
                f"未知的 stop_when 取值：{item!r}；"
                f"可用：{', '.join(sorted(STOP_WHEN_ALIASES))}"
            )
        flags[key] = True
    return flags


def _validate_roguelike_values(params: dict) -> None:
    if "difficulty" in params and _require_int("difficulty", params["difficulty"]) < -1:
        raise ValueError(f"difficulty 必须 >= -1（-1 表示不指定）：{params['difficulty']}")
    for name in ("starts_count", "investments_count"):
        if name in params and _require_int(name, params[name]) < 1:
            raise ValueError(f"{name} 必须 >= 1：{params[name]}")
    if "expected_collapsal_paradigms" in params:
        _require_str_list("expected_collapsal_paradigms", params["expected_collapsal_paradigms"])
    if "start_foldartal_list" in params:
        _require_str_list("start_foldartal_list", params["start_foldartal_list"])
    if "blackflow_start_animals" in params:
        animals = _require_str_list("blackflow_start_animals", params["blackflow_start_animals"])
        bad = sorted(set(animals) - set(BLACKFLOW_ANIMALS))
        if bad or len(animals) > 2 or animals.count("swaddled_feathered_serpent") > 1:
            raise ValueError(f"blackflow_start_animals 非法：{animals!r}；可选 {BLACKFLOW_ANIMALS}，最多 2 项且羽蛇至多 1 项")
    if "blackflow_start_rewards" in params:
        mask = _require_int("blackflow_start_rewards", params["blackflow_start_rewards"])
        if not 0 <= mask <= 4095:
            raise ValueError(f"blackflow_start_rewards 是 0..4095 位掩码：{mask}")
    if "blackflow_strategy" in params and params["blackflow_strategy"] not in BLACKFLOW_STRATEGIES:
        raise ValueError(f"blackflow_strategy 必须是 {BLACKFLOW_STRATEGIES}：{params['blackflow_strategy']!r}")
    if "blackflow_cultivation_target" in params:
        target = params["blackflow_cultivation_target"]
        if target not in BLACKFLOW_CULTIVATION_TARGETS:
            raise ValueError(f"blackflow_cultivation_target 必须是 {BLACKFLOW_CULTIVATION_TARGETS}：{target!r}")
    if "find_playTime_target" in params:
        target = _require_int("find_playTime_target", params["find_playTime_target"])
        if target not in PLAYTIME_TARGETS:
            raise ValueError(f"find_playTime_target 必须是 {PLAYTIME_TARGETS}（令/黍/年）：{target}")


def build_roguelike_params(theme: str = "", squad: str = "", operators=None,
                           stop_when=None, **maa_kw) -> dict:
    """生成 ``Roguelike`` 任务参数；空 theme/squad 表示交给 MAA 默认值。

    operators 支持干员名或 ``{"name", "use_support"}`` 对象，顺序对应开局三次招募。
    stop_when 支持 final_boss / max_level / investment_full（或原生键名）。
    """
    _check_kw(maa_kw, ROGUELIKE_PARAM_KEYS, "Roguelike", _ROGUELIKE_NAMED_KEYS)
    mode = _require_int("mode", maa_kw.pop("mode", 0))
    if mode in ROGUELIKE_REMOVED_MODES:
        raise ValueError(f"Roguelike mode={mode} 不可用：{ROGUELIKE_REMOVED_MODES[mode]}")
    if mode not in ROGUELIKE_MODE_THEMES:
        raise ValueError(f"Roguelike mode 必须属于 {ROGUELIKE_MODES}：{mode!r}")
    if theme and theme not in ROGUELIKE_THEMES:
        raise ValueError(f"未知肉鸽主题 {theme!r}；可选：{', '.join(ROGUELIKE_THEMES)}")
    allowed_themes = ROGUELIKE_MODE_THEMES[mode]
    if theme:
        if theme not in allowed_themes:
            raise ValueError(f"Roguelike mode={mode} 仅支持主题 {allowed_themes}，收到 {theme!r}")
    elif allowed_themes != ROGUELIKE_THEMES:
        raise ValueError(f"Roguelike mode={mode} 需显式指定 theme ∈ {allowed_themes}")
    if squad and not isinstance(squad, str):
        raise ValueError(f"squad 必须是字符串：{squad!r}")

    params: dict = {"mode": mode}
    if theme:
        params["theme"] = theme
    if squad:
        params["squad"] = squad
    if operators is not None:
        params["core_char_list"] = _core_char_list(operators)
    params.update(_stop_flags(stop_when))
    params.update(maa_kw)
    _validate_roguelike_values(params)
    return _finalize(params)


def build_sss_params(filename, *, loop_times: int = 1, **maa_kw) -> dict:
    """生成 ``SSSCopilot`` 任务参数；作业协议见 protocol/sss-schema。"""
    _check_kw(maa_kw, SSS_PARAM_KEYS, "SSSCopilot")
    params: dict = {
        "filename": _resolve_file(filename, "filename"),
        "loop_times": _require_int("loop_times", loop_times),
    }
    if params["loop_times"] < 1:
        raise ValueError(f"loop_times 必须 >= 1：{params['loop_times']}")
    params.update(maa_kw)
    return _finalize(params)


def _paradox_entry(item) -> dict:
    if isinstance(item, str):
        return {"filename": _resolve_file(item, "list")}
    if not isinstance(item, dict):
        raise ValueError(f"ParadoxCopilot list 条目必须是路径字符串或对象：{item!r}")
    unknown = set(item) - {"id", "filename"}
    if unknown:
        raise ValueError(f"ParadoxCopilot list 条目含未知字段 {sorted(unknown)}：{item!r}")
    entry = {"filename": _resolve_file(item.get("filename"), "list")}
    if item.get("id") is not None:
        if not isinstance(item["id"], str) or not item["id"]:
            raise ValueError(f"list 条目 id 必须是非空字符串：{item['id']!r}")
        entry["id"] = item["id"]
    return entry


def build_paradox_params(filename=None, *, paradox_list=None, **maa_kw) -> dict:
    """生成 ``ParadoxCopilot`` 任务参数；filename 与 list 必须二选一。"""
    _check_kw(maa_kw, PARADOX_PARAM_KEYS, "ParadoxCopilot")
    if filename is not None and paradox_list is not None:
        raise ValueError("ParadoxCopilot 的 filename 与 list 必须二选一，不能同时提供")
    if filename is not None:
        params = {"filename": _resolve_file(filename, "filename")}
    elif paradox_list is not None:
        if isinstance(paradox_list, (str, bytes, dict)) or not hasattr(paradox_list, "__iter__"):
            raise ValueError(f"paradox_list 必须是作业条目序列：{paradox_list!r}")
        entries = [_paradox_entry(item) for item in paradox_list]
        if not entries:
            raise ValueError("paradox_list 不能为空；单个作业请用 filename=")
        params = {"list": entries}
    else:
        raise ValueError("ParadoxCopilot 必须提供 filename 或 paradox_list 之一")
    params.update(maa_kw)
    return _finalize(params)


def _submit(kind: str, params: dict, asst, events_path) -> dict:
    """提交任务并摘要原生事件；asst 由调用方注入（已 load/connect）。"""
    task_type = MAA_TASK_TYPES[kind]
    append_task = getattr(asst, "append_task", None)
    if not callable(append_task):
        raise TypeError(f"asst 不支持 append_task，无法提交 {task_type}：{type(asst).__name__}")
    task_id = append_task(task_type, params)
    if not task_id:
        raise RuntimeError(f"MAA 拒绝添加 {task_type} 任务：{params}")
    events = None if events_path is None else Path(events_path).resolve()
    summary = None if events is None else parse_maa_events(events)
    ok = summary is None or summary["program_status"] not in {"failed", "timeout"}
    return {
        "ok": ok,
        "task_id": task_id,
        "mode": kind,
        "params": params,
        "events_path": None if events is None else str(events),
        "summary": summary,
    }


def run_roguelike_task(theme: str = "", squad: str = "", operators=None,
                       stop_when=None, *, asst, events_path=None, **maa_kw) -> dict:
    """提交原生 ``Roguelike`` 任务；不启动、不连接，生命周期由调用方管理。"""
    params = build_roguelike_params(theme=theme, squad=squad, operators=operators,
                                    stop_when=stop_when, **maa_kw)
    return _submit("roguelike", params, asst, events_path)


def run_sss_copilot(filename, *, loop_times: int = 1, asst, events_path=None, **maa_kw) -> dict:
    """提交原生 ``SSSCopilot``（保全派驻）任务。"""
    params = build_sss_params(filename, loop_times=loop_times, **maa_kw)
    return _submit("sss", params, asst, events_path)


def run_paradox_copilot(filename=None, *, paradox_list=None, asst, events_path=None,
                        **maa_kw) -> dict:
    """提交原生 ``ParadoxCopilot``（悖论模拟）任务；单个作业用 filename。"""
    params = build_paradox_params(filename, paradox_list=paradox_list, **maa_kw)
    return _submit("paradox", params, asst, events_path)
