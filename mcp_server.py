#!/usr/bin/env python3
"""明日方舟打关 MCP 服务（FastMCP）。

启动：
  .venv/Scripts/python.exe mcp_server.py                 # stdio JSON-RPC
  .venv/Scripts/python.exe mcp_server.py --http --port 8765

设计要点：
- **stdout 只走 JSON-RPC**。模块导入时就把 fd 1 重定向到 stderr，另存原始
  fd 供 Python 的 sys.stdout 使用，避免 MAA 的 C++ 日志污染协议管道。
- 所有日志走 logging(stderr)，不使用 print；`src/planner/intel` 的库路径同理，
  告警/缓存日志一律 `file=sys.stderr`，失败抛 Exception（不抛 SystemExit）。
- 工具默认只演习；非零理智关卡 `mode="official"` 必须显式 `authorize_official=True`，
  零理智关卡（apCost=0）的实际作战可按 official 直接执行。
- box/roster 读本地导出 `data/operators.json`（MAA 同步快照），不是在线实时同步。
- `--http` 默认只绑回环；绑定非回环地址必须显式 `--allow-remote`，否则拒绝启动
  （退出码 2）。HTTP 模式没有应用层认证，暴露即等于把全部工具（含实机执行）
  交给任意可达者。
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
from pathlib import Path
from urllib.error import URLError

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---------------------------------------------------------------- stdout 隔离
# 原生库（MaaCore）会直接往 fd 1 写字；先把它整体改道 stderr，再把真正的
# stdout 以 Python 包装形式交还给 sys.stdout，供 JSON-RPC 使用。
_real_stdout_fd = os.dup(1)
os.dup2(2, 1)
sys.stdout = io.TextIOWrapper(
    os.fdopen(_real_stdout_fd, "wb"), encoding="utf-8", line_buffering=True
)

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("arknights-mcp")

from mcp.server.fastmcp import FastMCP, Image  # noqa: E402

mcp = FastMCP(
    name="arknights-stage-ops",
    instructions=(
        "明日方舟打关工具集：查询关卡情报与实机通关状态、对比我方干员与参考作业、"
        "硬校验 Copilot 作业坐标、演习/正式执行作业并回收结构化死因与证据图。"
        "默认只跑演习（exercise），正式开打必须显式授权。"
        "零理智关卡（apCost=0，如教学/剧情节点）的实际作战可按 official 执行且无需授权；"
        "非零理智关卡的 official 必须 authorize_official=True。"
        "box/roster 来自本地导出 data/operators.json（MAA 同步快照），不是在线实时账号数据，"
        "sync_time 只是导出时间。"
    ),
)


# ------------------------------------------------------------------ 内部工具
def _stage_intel(stage_code: str, *, refresh: bool = False) -> dict:
    from src.planner.intel import build_compact_intel

    return build_compact_intel(stage_code, refresh=refresh)


def _probe(stage_code: str) -> dict:
    from src.engine.navigator import probe_stage_status

    return probe_stage_status(stage_code)


def _fail(message: str, **extra) -> dict:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    logger.warning("tool failure: %s", message)
    return payload


# --------------------------------------------------------------------- tools
@mcp.tool()
def lookup_knowledge(query: str = "", section: str = "", refresh: bool = False) -> str:
    """按需查询知识：本地知识卡 / MAA 协议 / PRTS 词条 / 空查询主题清单。

    Args:
        query: 本地知识卡名、MAA 协议主题或 PRTS 词条；空字符串列出可用主题。
        section: 章节关键词；空字符串返回全文。
        refresh: True 时刷新缓存。
    """
    from src.planner.doc_fetcher import lookup

    try:
        return lookup(query, section, refresh)
    except (LookupError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        return json.dumps(_fail(f"{type(exc).__name__}: {exc}"), ensure_ascii=False)


@mcp.tool()
def get_next_stage(stage_code: str) -> dict:
    """按关卡解锁图解析同区域下一关；无进度时只返回 blocked_by，不假定已通关。"""
    from src.planner.intel_access import resolve_next_stage

    try:
        return resolve_next_stage(stage_code)
    except (OSError, RuntimeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        return _fail(f"下一关解析失败: {type(exc).__name__}: {exc}", stage_code=stage_code)


@mcp.tool()
def get_stage_brief(stage_code: str) -> dict:
    """获取默认任务简报；完整地图、波次、敌人和机制请用 query_stage_intel 按需查询。"""
    from src.planner.intel_access import get_stage_brief as build_brief

    try:
        return build_brief(stage_code)
    except (OSError, RuntimeError, ValueError, LookupError, json.JSONDecodeError) as exc:
        return _fail(f"任务简报失败: {type(exc).__name__}: {exc}", stage_code=stage_code)


@mcp.tool()
def query_stage_intel(stage_code: str, section: str, key: str | None = None) -> dict:
    """按需读取 map/routes/timeline/enemies/mechanics/roster/references/raw 分段情报。"""
    from src.planner.intel_access import query_stage_intel as query_intel

    try:
        return query_intel(stage_code, section, key)
    except (OSError, RuntimeError, ValueError, LookupError, json.JSONDecodeError) as exc:
        return _fail(f"分段情报查询失败: {type(exc).__name__}: {exc}",
                     stage_code=stage_code, section=section, key=key)


@mcp.tool()
def get_enemy_visual(enemy: str, refresh: bool = False) -> list:
    """按敌人 key 或准确名称获取 PRTS 立绘/头像；未命中或模糊匹配不返回相似图。"""
    from src.planner.intel_access import get_enemy_visual as query_visual

    try:
        result = query_visual(enemy, refresh=refresh)
    except (OSError, RuntimeError, ValueError, LookupError, json.JSONDecodeError) as exc:
        return [_fail(f"敌人视觉资料查询失败: {type(exc).__name__}: {exc}", enemy=enemy)]
    if not result.get("ok"):
        return [result]
    return [json.dumps(result, ensure_ascii=False), Image(path=result["image_path"])]


@mcp.tool()
def get_stage_status_and_intel(stage_code: str, probe_live: bool = False) -> dict:
    """查询关卡紧凑情报（文本地图/波次/Boss/参考作业）+ 关卡范式画像。

    返回中的 `profile` 是一期核心契约：`family`（ex_raid/single/tr_fixed/ep_tuning/
    ep_hazard_sandbox）、`target_modes`、`zero_sanity`、`fixed_squad`、`predefined_ops`。
    `predefined_ops` 非空表示系统预设编队（如 TR 关），此时 `box.source == "override"`。

    Args:
        stage_code: 关卡代号，如 TL-EX-8 或 stageId act55side_ex08。
        probe_live: True 时连接设备，点击关卡/难度条目做原生探测（默认 False，纯离线）。
            **这不是纯只读操作**：会产生点击与界面跳转，但不会按「开始行动」。
            探测返回的 `next_mode` ∈ NORMAL / RAID / ALL_DONE / UNKNOWN；
            `single` 家族三星后直接 ALL_DONE，不会去找突袭按钮；
            `execution_policy` 给出建议执行方式（演习/零理智正式/是否跳过选人）。
    """
    try:
        intel = _stage_intel(stage_code)
    except Exception as exc:  # noqa: BLE001 - 交给 MCP 客户端看原因，不吞掉
        return _fail(f"情报构建失败: {exc}", stage_code=stage_code)

    payload = dict(intel)
    if probe_live:
        try:
            payload["live_status"] = _probe(stage_code)
        except Exception as exc:  # noqa: BLE001
            payload["live_status"] = None
            payload["live_status_error"] = f"{type(exc).__name__}: {exc}"
        logger.info("probe %s -> %s", stage_code, payload["live_status"])
    return payload


@mcp.tool()
def get_box_and_references(stage_code: str) -> dict:
    """返回全部已拥有干员（eligible 仅标练度）、MAA 同步时间与参考作业适配。

    roster 是紧凑清单；实际技能等级/专精/模组未知，详细理论机制查 query_operator_specs。
    box 读本地导出 data/operators.json（MAA 同步快照），不是在线实时同步；
    sync_time 只是该导出文件的同步时间，不能当作当前账号状态。
    参考作业只提供思路，不默认保留原阵容；由 AI 比较全部已拥有候选。
    """
    try:
        intel = _stage_intel(stage_code)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"情报构建失败: {exc}", stage_code=stage_code)

    return {
        "stage": intel.get("stage"),
        "kill_total": intel.get("kill_total"),
        "box": intel.get("box", {}),
        "roster": intel.get("roster", []),
        "references": intel.get("references", []),
        "references_source": intel.get("references_source", "unknown"),
    }


@mcp.tool()
def validate_copilot_plan(copilot_json_path_or_str: str) -> dict:
    """硬校验 Copilot 作业：每个 Deploy 的落点必须匹配干员站位（MELEE/RANGED）。

    Args:
        copilot_json_path_or_str: 作业文件路径；也可以直接传 JSON 字符串。
    """
    from src.engine.validator import validate_job

    try:
        result = validate_job(copilot_json_path_or_str)
    except Exception as exc:  # noqa: BLE001
        return _fail(f"校验执行失败: {exc}")
    logger.info("validate ok=%s checked=%s", result.get("ok"), result.get("checked"))
    return result


@mcp.tool()
def see_screen() -> list:
    """获取新鲜的 MAA 原生截图供 Vision 观察；只截图、不点击、不触发战斗。"""
    from src.engine.navigator import capture_screen

    try:
        screen = capture_screen()
        summary = (f"当前画面 {screen['width']}×{screen['height']}，"
                   f"PNG {screen['bytes']} 字节；路径：{screen['path']}")
        return [summary, Image(path=screen["path"])]
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        return [_fail(f"截图失败: {type(exc).__name__}: {exc}")]


@mcp.tool()
def tap_text(text: str) -> dict:
    """用 MAA 原生 OCR 命中文字后点击；未命中返回结构化 LookupError。"""
    from src.engine.navigator import tap_text as native_tap_text

    try:
        return native_tap_text(text)
    except (LookupError, OSError, RuntimeError, ValueError, ImportError) as exc:
        return _fail(f"文字点击失败: {type(exc).__name__}: {exc}", text=text)


@mcp.tool()
def tap_point(x: int, y: int) -> dict:
    """仅点击 Vision 定位的纯图标/无文字遮罩；坐标使用截图的 1280×720 基准。"""
    from src.engine.navigator import tap_point as native_tap_point

    try:
        return native_tap_point(x, y)
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        return _fail(f"坐标点击失败: {type(exc).__name__}: {exc}", point=[x, y])


@mcp.tool()
def skip_story_phase(phase: str = "pre") -> dict:
    """跳过战前 pre 或战后 post 剧情；无剧情必须由原生 EndOfPlot 标记确认。"""
    from src.engine.navigator import skip_story

    try:
        return skip_story(phase=phase)
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        return _fail(f"跳剧情失败: {type(exc).__name__}: {exc}", phase=phase)


@mcp.tool()
def review_battle_frames(
    run_dir: str,
    start_s: float,
    end_s: float | None = None,
    interval_s: float | None = 0.5,
    time_base: str = "battle",
) -> list:
    """按指定时间从录像抽图供只识图的 AI 复核；单点 end_s=None，逐帧 interval_s=None。"""
    from src.engine.battle_review import extract_review_frames

    try:
        result = extract_review_frames(run_dir, start_s, end_s, interval_s, time_base)
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        return [_fail(f"战斗图片复核失败: {type(exc).__name__}: {exc}", run_dir=run_dir)]
    metadata = {**result, "frames": [
        {key: value for key, value in frame.items() if key != "frame_path"}
        for frame in result["frames"]
    ]}
    contents: list = [json.dumps(metadata, ensure_ascii=False)]
    contents.extend(Image(path=frame["frame_path"]) for frame in result["frames"])
    return contents


@mcp.tool()
def execute_stage_plan(
    copilot_json_path: str,
    mode: str = "exercise",
    authorize_official: bool = False,
    stage_code: str | None = None,
    timeout: float = 600.0,
    record_video: bool = False,
    allow_leak: bool = False,
) -> list:
    """执行 Copilot 作业。默认演习；正式模式必须显式授权。

    执行策略由关卡画像决定：零理智关的实际作战可使用 official，无需授权；
    formation 始终仅处理编队，不会转为开战。系统预设关卡的实际作战跳过选人。
    作业与请求关卡必须一致，执行器在设备连接前强制完成静态校验。
    失败链仅在有原生剧情证据且未结束时恢复一次，返回 story_recovery/needs_retry；
    不自动重跑作业、不把跳剧情完成当成战斗胜利。可用 see_screen 观察后再决策。


    Args:
        copilot_json_path: 作业 JSON 路径。
        mode: exercise（默认，不耗理智）/ official（正式，需授权）/ formation（仅编队）。
        authorize_official: 非零理智关卡的 official 模式必须为 True。
        stage_code: 关卡代号；省略时从作业 stage_name 推断。
        timeout: 硬超时秒数。
        record_video: 同时用 adb screenrecord 录屏，保存为 battle.mp4（默认关闭）。
        allow_leak: True 时允许漏怪继续（默认 False：检测到漏怪后尽快停止 Copilot 并尝试原生放弃；理智是否返还以游戏结算为准，非保证）。
    """
    if mode not in {"exercise", "official", "formation"}:
        return [_fail(f"非法 mode: {mode}", allowed=["exercise", "official", "formation"])]
    path = Path(copilot_json_path)
    if not path.exists():
        return [_fail(f"作业文件不存在: {path}")]

    from src.planner.intel import resolve_stage_profile
    from src.engine.runner import run_copilot
    from src.engine.navigator import recover_stuck_chain

    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        job_stage = job["stage_name"]
        profile = resolve_stage_profile(job_stage)
        if stage_code is not None:
            requested = resolve_stage_profile(stage_code)
            if requested["stage_id"] != profile["stage_id"]:
                return [_fail("作业 stage_name 与请求关卡不一致",
                              requested_stage=stage_code, job_stage=job_stage)]
        stage_code = profile["stage_code"]
    except (OSError, ValueError, KeyError, TypeError, LookupError) as exc:
        return [_fail(f"作业关卡解析失败: {type(exc).__name__}: {exc}")]
    zero_sanity = bool(profile["zero_sanity"])
    if mode == "official" and not zero_sanity and not authorize_official:
        return [_fail("拒绝执行：official 模式会消耗真实理智，需 authorize_official=True 显式授权")]

    try:
        report = run_copilot(
            stage_code,
            str(path),
            mode=mode,
            timeout=timeout,
            record_video=record_video,
            allow_leak=allow_leak,
        )
        # Copilot 自带剧情处理；失败时只恢复一次剧情，绝不自动重跑并再次扣理智。
        if mode in {"exercise", "official"}:
            recovery = recover_stuck_chain(report)
            if recovery["attempted"]:
                report["story_recovery"] = recovery
                report["needs_retry"] = True
    except (OSError, RuntimeError, ValueError, LookupError, ImportError) as exc:
        return [_fail(f"执行异常: {type(exc).__name__}: {exc}", stage_code=stage_code)]

    if report.get("ok"):
        return [report]

    failure = report.get("failure") or {}
    text = (
        f"执行失败 stage={stage_code} mode={mode}\n"
        f"死因: {failure.get('reason') or '未知'}\n"
        f"最后动作: #{failure.get('last_action_index')} {failure.get('last_action') or '-'}\n"
        f"事件流: {report.get('events_path')}"
    )
    contents: list = [text, report]
    evidence = report.get("evidence_png")
    if evidence and Path(evidence).exists():
        contents.append(Image(path=evidence))
    else:
        logger.info("no evidence png for failed run: %s", evidence)
    return contents


@mcp.tool()
def query_operator_specs(names: list[str] | None = None, role_filter: str = "",
                         owned_only: bool = True) -> dict:
    """查询账号实际练度和干员机制；技能等级/专精/模组未知，理论末级效果在 max。

    技能 max 中 sp_init 是落地已有的初始技力，sp_cost 是释放所需技力，单位均为技力点；
    duration 的单位为秒。sp_init 不是初动时间。
    仅自然回复且无额外技力效果时，理论首放等待秒数为
    max(0, sp_cost - sp_init) / 技力回复速度（点/秒）；攻击/受击回复不能套用。

    Args:
        names: 干员名列表；省略则查询全部已拥有干员。
        role_filter: 中文职业或 PROFESSION 枚举（如 狙击 / SNIPER），大小写不敏感。
        owned_only: 默认 True，避免把未拥有角色当候选；False 可查看全部角色理论机制。
    """
    from src.planner.intel import query_operator_specs as impl

    try:
        return impl(names, role_filter, owned_only=owned_only)
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return _fail(f"干员查询失败: {type(exc).__name__}: {exc}")


@mcp.tool()
def request_human_demo(stage_code: str, duration_s: float = 20.0) -> list:
    """请求人类示范：只读监听 adb 触控 duration_s 秒，反算为战场格与朝向。

    不做任何注入点击；返回动作流 dict 与示教连环画 PNG（存在时）。
    """
    from src.engine.teach_recorder import record_human_demo

    try:
        demo = record_human_demo(stage_code, duration_s)
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        return [_fail(f"示教采集失败: {type(exc).__name__}: {exc}", stage_code=stage_code)]
    contents: list = [demo]
    strip = demo.get("strip_path")
    if strip and Path(strip).exists():
        contents.append(Image(path=strip))
    return contents


def _special_session(kind: str):
    """连接模拟器并打开事件文件；调用方负责 stop 与 close。"""
    import time as _time

    from src.engine.navigator import _connect

    stamp = _time.strftime("%Y%m%d-%H%M%S")
    outdir = ROOT / "data" / "captures" / f"special-{kind}-{stamp}"
    outdir.mkdir(parents=True, exist_ok=True)
    events_path = outdir / "maa-events.jsonl"
    handle = events_path.open("w", encoding="utf-8")

    def _callback(message, details, _arg):
        try:
            text = details.decode("utf-8", "replace") if details else "{}"
            handle.write(json.dumps({"message": int(message), "details": text},
                                    ensure_ascii=False) + "\n")
            handle.flush()
        except (OSError, ValueError, TypeError) as exc:  # 回调不能抛回原生库
            logger.warning("特殊玩法事件写入失败: %s", exc)

    asst = _connect(_callback, maa_user=ROOT / "data/maa_userdata")
    return asst, events_path, handle


def _run_special(kind: str, submit, timeout: float) -> dict:
    """提交特殊玩法任务并等待结束，返回原生事件摘要。"""
    import time as _time

    from src.engine.recorder import parse_maa_events

    asst, events_path, handle = _special_session(kind)
    try:
        result = submit(asst, events_path)
        if not result.get("ok"):
            return result
        if not asst.start():
            raise RuntimeError("MAA start 失败")
        deadline = _time.monotonic() + timeout
        while asst.running():
            if _time.monotonic() > deadline:
                asst.stop()
                raise RuntimeError(f"任务超时（{timeout}s）")
            _time.sleep(0.3)
        result["summary"] = parse_maa_events(events_path)
        return result
    except (OSError, RuntimeError, ValueError, LookupError, ImportError) as exc:
        return _fail(f"{kind} 执行失败: {type(exc).__name__}: {exc}")
    finally:
        try:
            asst.stop()
        finally:
            handle.close()


@mcp.tool()
def run_roguelike(theme: str = "", squad: str = "", operators: list[dict] | None = None,
                  stop_when: dict | None = None, timeout: float = 1800.0) -> dict:
    """运行一次集成战略（MAA 原生 Roguelike）。主题如 Phantom/Sami/Sarkaz/JieGarden。"""
    from src.engine.special_modes import run_roguelike_task

    return _run_special(
        "roguelike",
        lambda asst, events_path: run_roguelike_task(
            theme, squad, operators, stop_when, asst=asst, events_path=events_path),
        timeout,
    )


@mcp.tool()
def run_sss_copilot(filename: str, loop_times: int = 1, timeout: float = 1800.0) -> dict:
    """运行保全派驻作业（MAA 原生 SSSCopilot）。"""
    from src.engine.special_modes import run_sss_copilot

    return _run_special(
        "sss",
        lambda asst, events_path: run_sss_copilot(
            filename, loop_times=loop_times, asst=asst, events_path=events_path),
        timeout,
    )


@mcp.tool()
def run_paradox_copilot(filename: str | None = None,
                        paradox_list: list[dict] | None = None,
                        timeout: float = 1800.0) -> dict:
    """运行悖论模拟（MAA 原生 ParadoxCopilot）。filename 与 paradox_list 二选一。"""
    from src.engine.special_modes import run_paradox_copilot

    return _run_special(
        "paradox",
        lambda asst, events_path: run_paradox_copilot(
            filename, paradox_list=paradox_list, asst=asst, events_path=events_path),
        timeout,
    )


@mcp.prompt()
def arknights_stage_sop(stage_code: str, goal: str = "三星通关") -> str:
    """标准打关指挥 SOP：漏怪即停并尝试原生放弃、盲区请求人类示教。"""
    return f"""你是明日方舟打关指挥官，目标：{stage_code} · {goal}。按以下顺序执行，禁止跳步。

1. 情报：先 get_stage_brief("{stage_code}")；需要地图、波次、敌人或机制时，用 query_stage_intel 按 section/key 查询；敌人认图用 get_enemy_visual。不要默认拉取全量情报。
2. 作业：get_box_and_references("{stage_code}") 取得全部已拥有 roster 与参考作业。参考作业只提供思路；即使原角色已拥有，也要比较全部已拥有候选，由你这个 AI 判断阵容与排轴，工具不默认保留原阵容。需要详细技能机制时 query_operator_specs；actual 中技能等级/专精/模组为 unknown，max 只是理论末级效果，不能当成账号实际达标。
3. 校验：validate_copilot_plan(<作业>)；有 error 必须先修作业，不许带病开打。
4. 手动确认在关卡面板，不自动导航；核对目标关卡 {stage_code} 与「开始行动」后再继续。
5. 试跑：execute_stage_plan(<作业>, mode="exercise")。演习不耗理智。
6. 正式：仅当演习稳定且用户明确授权，才 execute_stage_plan(..., mode="official", authorize_official=True)。
   - 漏怪即停（allow_leak=False）：检测到漏怪后尽快停止并尝试原生放弃，目标是减少损失。
     返回 battle_result="abandoned_on_leak" 时，读 evidence_png 复盘，修改部署/技能轴后重试，
     不要降级为硬打。放弃是否成功、理智是否返还都以游戏结算为准，不能假定已全额返还。
7. 失败/放弃后先看 failure.reason 与 maa-events.jsonl，
   再用 review_battle_frames 按 battle/leak 时间抽取预览帧；只识图，不直接输入视频。
   定位首次异常后修改作业并归位，再进入下一轮。归位/退出未确认或结果为 ABANDON_FAILED、
   EXIT_UNCONFIRMED 时，需人工查看新截图处理。
8. 遇到无法识别的界面/纯图标按钮：see_screen 截图判断；仍不确定就
   request_human_demo("{stage_code}", 20) 请求人类示范，再按动作流执行。
9. 任何一步失败都要给出：失败原因、最后动作、证据路径；不要静默重试超过一次。
"""


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_host(host: str) -> bool:
    """HTTP 绑定地址是否回环：127.0.0.1 / ::1 / localhost（大小写不敏感）。"""
    if not isinstance(host, str):
        return False
    name = host.strip()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    return name.lower() in LOOPBACK_HOSTS


def validate_http_bind(host: str, allow_remote: bool = False) -> str | None:
    """校验 HTTP 绑定。放行返回 None，拒绝返回可直接展示的错误文案。

    HTTP 模式没有应用层认证，非回环绑定等于把全部 MCP 工具（含实机执行）
    交给任意可达者，因此必须显式 `--allow-remote` 才放行。
    """
    if is_loopback_host(host) or allow_remote:
        return None
    return (
        f"拒绝以 --http 绑定非回环地址 {host!r}：HTTP 模式没有应用层认证，"
        f"任何能连到该端口的人都能调用全部 MCP 工具（含实机执行与正式作战）。"
        f"确需远程访问请显式加 --allow-remote，并自行承担风险。"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="明日方舟打关 MCP 服务")
    ap.add_argument("--http", action="store_true", help="以 streamable-http 启动（默认 stdio）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--allow-remote",
        action="store_true",
        help="允许 --http 绑定非回环地址（无应用层认证，风险自担）",
    )
    args = ap.parse_args(argv)

    if args.http:
        error = validate_http_bind(args.host, allow_remote=args.allow_remote)
        if error:
            logger.error("%s", error)
            return 2
        if not is_loopback_host(args.host):
            logger.warning(
                "HTTP 绑定非回环地址 %s 且无应用层认证：任何可达者都能调用全部 MCP 工具",
                args.host,
            )
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        logger.info("starting streamable-http on %s:%s/mcp", args.host, args.port)
        mcp.run(transport="streamable-http")
    else:
        logger.info("starting stdio server")
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
