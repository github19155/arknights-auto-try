"""MAA 原生面板探测、战斗归位、剧情恢复与 Vision 交互桥。

探测与点击均由 Custom 任务完成；截图只读，不启动行动。
剧情缺失必须由原生 EndOfPlot 确认，链完成不等于战斗胜利。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parents[2]
OVERLAY = ROOT / "tools" / "maa-resources" / "probe-and-nav"
NAV_OVERLAY = "global-nav"
DIFFICULTY_OVERLAY = "ep-difficulty"
# 原生画面探测的 Mark → 画面名；顺序即 _screen_of 的优先级。
SCREEN_MARKS = (("MarkNavScreenResult", "result"), ("MarkNavScreenStory", "story"),
                ("MarkNavScreenPanel", "panel"), ("MarkNavScreenTerminal", "terminal"),
                ("MarkNavScreenActivity", "activity"), ("MarkNavScreenMain", "main"))
# 探测明确判定「不属于任何已知画面」的终点；它同样证明剧情模板没有命中。
SCREEN_UNKNOWN_MARK = "MarkNavScreenUnknown"
# 战后剧情链复用原生《跳过》overlay 的模板与节点。
STORY_SKIP_OVERLAY = "story-skip"
# 真正代表「点到了跳过/确认控件」的节点；EndOfPlot 只是附带证据。
STORY_CLICK_NODES = ("SkipBattlePlot", "SkipBattlePlotConfirm")
STORY_SKIP_TEXTS = ("跳过",)
STORY_CONFIRM_TEXTS = ("确认", "確認")

MARKS = {
    "MarkNormalDone", "MarkNormalNotDone", "MarkChallengeLocked",
    "MarkChallengeNotDone", "MarkChallengeDone", "MarkChallengePanel",
    "MarkStageMissing",
}
FAMILIES = ("single", "ex_raid", "tr_fixed", "ep_tuning", "ep_hazard_sandbox")
# 9~14 章磨难在章节总览切，15 章起的险地在关卡面板切。
DIFFICULTY_FAMILIES = {"ep_tuning": "tough", "ep_hazard_sandbox": "hazard"}


class StorySkipError(RuntimeError):
    """原生剧情链已结束，但没有确认 EndOfPlot。"""


def _stage_info(stage_code: str, maa_dir: Path) -> dict:
    """仅查本地表；同代号的复刻关优先采用当前 MAA 资源的映射。"""
    with (ROOT / "data/gamedata_cache/stage_table.json").open(encoding="utf-8") as source:
        stages = json.load(source)["stages"]
    code = stage_code.strip()
    if not code:
        raise ValueError("关卡代号不能为空")
    if code in stages:
        return stages[code]
    maa_stages = maa_dir / "resource/stages.json"
    if maa_stages.is_file():
        with maa_stages.open(encoding="utf-8") as source:
            for entry in json.load(source):
                if (entry.get("code") or "").upper() == code.upper():
                    return stages[entry["stageId"]]
    for entry in stages.values():
        if (entry.get("code") or "").upper() == code.upper() and entry["difficulty"] == "NORMAL":
            return entry
    raise ValueError(f"本地关卡表中不存在关卡：{stage_code}")


def _connect(callback, *, runtime_overlay: Path | None = None, overlay=None,
             maa_dir=None, adb=None, serial=None, maa_user=None):
    """加载基础和指定增量资源并连接；调用方负责 stop 和释放实例。"""
    maa_dir = Path(maa_dir or os.environ.get("MAA_DIR", "maa"))
    python_dir = str(maa_dir / "Python")
    if python_dir not in sys.path:
        sys.path.insert(0, python_dir)
    from asst.asst import Asst
    from asst.utils import InstanceOptionType

    user_dir = Path(maa_user) if maa_user is not None else ROOT / "data/maa_userdata"
    user_dir.mkdir(parents=True, exist_ok=True)
    if not Asst.load(str(maa_dir), user_dir=str(user_dir.resolve())):
        raise RuntimeError(f"MAA 基础资源加载失败：{maa_dir}")
    # 探测默认加载专用 overlay；其他 Custom 链显式指定资源，避免污染开始按钮。
    resources = []
    for item in overlay if isinstance(overlay, (list, tuple)) else (overlay,):
        if item is not None:
            resources.append(item)
    if runtime_overlay is not None:
        resources.append(runtime_overlay)
    for resource in resources:
        if not Asst.load(str(resource)):
            raise RuntimeError(f"MAA 增量资源加载失败：{resource}")
    asst = Asst(callback=Asst.CallBackType(callback))
    connected = False
    try:
        if not asst.set_instance_option(InstanceOptionType.touch_type, "maatouch"):
            raise RuntimeError("MAA 无法启用 MaaTouch")
        if not asst.connect(
            str(adb or os.environ.get("MAA_ADB", "adb")),
            str(serial or os.environ.get("MAA_SERIAL", "127.0.0.1:5555")),
            "MuMuEmulator12",
        ):
            raise RuntimeError("MAA 连接模拟器失败")
        connected = True
        return asst
    finally:
        if not connected:
            try:
                if not asst.stop():
                    raise RuntimeError("MAA 连接失败后停止实例失败")
            finally:
                del asst


def _probe_mark_allowed(state: dict, *, task_id: int, entry: str) -> bool:
    """只有当前 Custom 链里 entry 的原生识别未命中才能映射成答案。

    MAA 在识别越界后发 SubTaskError(20000, 带 first/pre_task)，随后以
    TaskChainError(10000) 收尾；只有前者能证明「识别不到」。callback 解析失败
    或 0/1 内部错误记在 state["error"]，其他 taskid 的错误一律不参与判定。
    """
    if state["error"] is not None:
        return False
    misses = [error for error in state["chain_errors"]
              if error["taskid"] == task_id and error["message"] == 20000]
    return bool(misses) and all(entry in error["tasks"] for error in misses)


def _run_probe(stage_code: str, *, challenge: bool, skip_restore: bool = False, maa_dir=None,
               adb=None, serial=None, maa_user=None) -> dict:
    """skip_restore 用于磨难/险地面板：原生 ProbeRestoreNormal 会把难度点回标准，必须禁用。"""
    maa_dir = Path(maa_dir or os.environ.get("MAA_DIR", "maa"))
    stage = _stage_info(stage_code, maa_dir)
    code = stage["code"]
    safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", code)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    outdir = ROOT / "data/captures" / f"probe-{safe_code}-{stamp}"
    runtime_overlay = outdir / "runtime-overlay"
    resource_dir = runtime_overlay / "resource"
    resource_dir.mkdir(parents=True)
    # 运行期资源只绑定当前关卡关键词；共享任务中不包含任何特定关卡。
    # MAA 的 overlay 是「按节点名整条替换」，所以这里必须写**完整节点**，
    # 不能只给 {"text": [...]}，否则 algorithm/roi/next 会一起丢失。
    base_tasks = json.loads((OVERLAY / "resource/tasks.json").read_text(encoding="utf-8"))
    runtime_tasks = {}
    for name in ("ProbeClickStage", "ProbeConfirmStage"):
        node = dict(base_tasks[name])
        # 地图上的关卡代号标签与面板标题是两处文字：点击用代号，确认只用中文名，
        # 否则代号在两张画面上都出现，会把「没找到面板」误判成已确认。
        # 两处都用代号：地图上的代号在 ROI 外，面板标题区的代号可唯一定位。
        # 中文名对「致……」这类含省略号的关卡 OCR 不稳定，不能作为确认依据。
        node["text"] = [code]
        # 标题区 OCR 会把左侧装饰读进同一串（实测 "NTL-10"），代号用子串匹配；
        # 地图上的标签仍用整串匹配，避免 "TL-1" 命中 "TL-10"。
        node["fullMatch"] = name == "ProbeClickStage"
        runtime_tasks[name] = node
    if skip_restore:
        # 磨难/险地面板上不存在「普通难度/标准模式」，原来的点击会把难度打回标准。
        runtime_tasks["ProbeRestoreNormal"] = {
            "algorithm": "JustReturn", "action": "DoNothing", "next": ["ProbeConfirmStage"]}
    with (resource_dir / "tasks.json").open("w", encoding="utf-8") as target:
        json.dump(runtime_tasks, target, ensure_ascii=False, indent=2)

    state = {"marks": [], "missing": False, "panel_open": False, "error": None,
             "chain_errors": []}
    lock = threading.Lock()
    events_path = outdir / "maa-events.jsonl"
    with events_path.open("w", encoding="utf-8") as events:
        def callback(message, details, _arg):
            # ctypes 回调中的异常不会传给调用者，必须记录后由轮询线程抛出。
            with lock:
                try:
                    text = details.decode("utf-8") if details else ""
                    events.write(json.dumps({"message": int(message), "details": text},
                                            ensure_ascii=False) + "\n")
                    events.flush()
                    parsed = json.loads(text) if text else {}
                    if message in (0, 1):
                        # 内部错误/初始化失败没有可用的识别结论，只能抛给调用方。
                        state["error"] = RuntimeError(f"MAA 探测任务错误：{text}")
                    elif message in (10000, 20000):  # TaskChainError / SubTaskError
                        # 只有 SubTaskError 带失败节点；TaskChainError 只是链级尾音。
                        detail = parsed.get("details") or {}
                        tasks = []
                        for name in (*(parsed.get("first") or []), parsed.get("pre_task"),
                                     detail.get("task")):
                            if name and name not in tasks:
                                tasks.append(name)
                        state["chain_errors"].append(
                            {"message": message, "taskid": parsed.get("taskid"),
                             "tasks": tasks})
                    if message not in (20001, 20002):  # SubTaskStart / SubTaskCompleted
                        return
                    node = parsed.get("details", {}).get("task", "")
                    if node in MARKS:
                        if node not in state["marks"]:
                            state["marks"].append(node)
                    elif node == "MarkStageMissing":
                        state["missing"] = True
                    elif node == "ProbeConfirmStage" and message == 20002:
                        # 该节点只能在 StartButton1 模板匹配后进入，并再次校验标题。
                        state["panel_open"] = True
                except (OSError, ValueError, TypeError, AttributeError) as error:
                    state["error"] = error

        asst = None
        try:
            asst = _connect(callback, overlay=OVERLAY, runtime_overlay=runtime_overlay,
                            maa_dir=maa_dir, adb=adb, serial=serial, maa_user=maa_user)
            deadline = time.monotonic() + 60

            def execute(entry: str, error_mark: str | None = None) -> list[str]:
                with lock:
                    offset = len(state["marks"])
                task_id = asst.append_task("Custom", {"task_names": [entry]})
                if not task_id:
                    raise RuntimeError(f"MAA 无法添加探测任务：{entry}")
                if not asst.start():
                    raise RuntimeError(f"MAA 无法启动探测任务：{entry}")
                while True:
                    with lock:
                        error = state["error"]
                    if error is not None:
                        raise RuntimeError(
                            f"MAA 探测事件处理失败；日志：{events_path}") from error
                    if not asst.running():
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError(f"MAA 探测超过 60 秒；日志：{events_path}")
                    time.sleep(0.1)
                # 任务结束后重新取回终点回调，不能只依据 running() 的完成信号；
                # 映射也必须等链真正收尾，避免 20000 之后还有致命回调。
                with lock:
                    if state["error"] is not None:
                        raise RuntimeError(f"MAA 探测任务失败；日志：{events_path}") from state["error"]
                    mapped = (error_mark is not None and
                              _probe_mark_allowed(state, task_id=task_id, entry=entry))
                    if mapped:
                        state["marks"].append(error_mark)
                    chain_errors = [error for error in state["chain_errors"]
                                    if error["taskid"] == task_id]
                    marks = state["marks"][offset:]
                    missing = state["missing"]
                if mapped:
                    return [error_mark]
                if chain_errors:
                    # 不是本次 ProbeFirstDropCheck 的识别未命中：0/1、坏 JSON、
                    # 其他 taskid 或其他节点的错误都不能伪装成通关答案。
                    raise RuntimeError(f"MAA 探测任务失败（{entry}）；日志：{events_path}")
                if not marks and not missing:
                    raise RuntimeError(f"MAA 探测结束但没有命中 Mark 节点；日志：{events_path}")
                return marks

            normal_marks = execute("ProbeCheck")
            if "MarkStageMissing" in normal_marks or state["missing"]:
                if challenge:
                    raise RuntimeError(f"未找到并校验关卡详情面板：{code}；日志：{events_path}")
            elif challenge and "MarkNormalDone" in normal_marks:
                # 每个 Mark 都是 Stop 终点，因此普通与突袭分成多条 Custom 任务，
                # 共用同一个 60 秒截止时间及连接，绝不越过终点继续执行。
                challenge_marks = execute("ProbeChallengeEntry")
                if "MarkChallengePanel" in challenge_marks:
                    # 突袭面板已在眼前：有「首次掉落」= 未通关；识别不到 = 已通关。
                    execute("ProbeFirstDropCheck", error_mark="MarkChallengeDone")
            with lock:
                return {"marks": list(state["marks"]), "missing": state["missing"],
                        "panel_open": state["panel_open"], "stage": stage,
                        "runtime_overlay": str(runtime_overlay.resolve()),
                        "events_path": str(events_path.resolve())}
        finally:
            if asst is not None:
                try:
                    if not asst.stop():
                        raise RuntimeError(f"MAA 探测实例停止失败；日志：{events_path}")
                finally:
                    # Python 绑定的析构函数调用 AsstDestroy，释放原生连接。
                    del asst


def probe_stage_status(stage_code: str, *, maa_dir=None, adb=None, serial=None, maa_user=None) -> dict:
    """读取关卡画像并探测当前面板；不支持的关卡族显式报错。"""
    from src.planner.intel import resolve_stage_profile

    profile = resolve_stage_profile(stage_code, maa_dir=maa_dir)
    family = profile["family"]
    if family not in FAMILIES:
        raise NotImplementedError(f"关卡 {stage_code} 的面板状态探测尚未接入：{family}")

    # 磨难/险地面板没有「普通难度」按钮，原生 ProbeRestoreNormal 会把难度点回标准。
    result = _run_probe(profile["stage_code"], challenge=family == "ex_raid",
                        skip_restore=family in DIFFICULTY_FAMILIES,
                        maa_dir=maa_dir, adb=adb, serial=serial, maa_user=maa_user)
    marks = result["marks"]
    if result["missing"] or not result["panel_open"] or "MarkStageMissing" in marks:
        raise RuntimeError(f"未找到并校验关卡详情面板：{stage_code}；日志：{result['events_path']}")
    cleared = []
    if "MarkNormalDone" in marks:
        cleared.append("NORMAL")
        next_mode = "ALL_DONE"
        if family == "ex_raid":
            if "MarkChallengeDone" in marks:
                cleared.append("RAID")
            elif "MarkChallengeLocked" in marks:
                # 突袭未解锁时没有任何可执行的下一步；UNKNOWN 保留 locked 证据。
                next_mode = "UNKNOWN"
            elif "MarkChallengeNotDone" in marks:
                next_mode = "RAID"
            else:
                raise RuntimeError(f"突袭探测缺少终点标记：{stage_code}；日志：{result['events_path']}")
    elif "MarkNormalNotDone" in marks:
        next_mode = "NORMAL"
    else:
        raise RuntimeError(f"普通探测缺少终点标记：{stage_code}；日志：{result['events_path']}")

    difficulty = None
    if family in DIFFICULTY_FAMILIES:
        expected = DIFFICULTY_FAMILIES[family]
        state = _difficulty_state(maa_dir=maa_dir, adb=adb, serial=serial, maa_user=maa_user)
        if state["state"] not in {expected, "unknown"}:
            raise RuntimeError(f"关卡 {stage_code} 当前难度是 {state['state']}，需要 {expected}；"
                               f"日志：{state['events_path']}")
        # unknown 是「没识别到」而不是「已确认」，原样上报，由调用方决定是否重试。
        difficulty = {"expected": expected, "state": state["state"], "marks": state["marks"]}

    zero_sanity = profile["zero_sanity"]
    return {
        "stage_code": profile["stage_code"], "stage_id": profile["stage_id"],
        "family": family,
        "target_modes": ["NORMAL"] if next_mode == "NORMAL" else list(profile["target_modes"]),
        "cleared_modes": cleared, "next_mode": next_mode,
        "execution_policy": {
            "mode": "official" if zero_sanity else "exercise",
            "skip_formation": family == "tr_fixed" or profile["fixed_squad"],
            "allow_official": bool(zero_sanity),
            "reason": "apCost=0" if zero_sanity else "apCost>0：默认演习，正式需显式授权",
        },
        "difficulty": difficulty,
        "panel_verified": True,
        "evidence": {"marks": list(marks), "events_path": result["events_path"]},
    }


def _overlay_paths(overlay) -> list[Path]:
    """单个 overlay 或 overlay 列表都解析成绝对路径；保持加载顺序。"""
    if overlay is None:
        return []
    paths = []
    for item in overlay if isinstance(overlay, (list, tuple)) else (overlay,):
        path = Path(item)
        if not path.is_absolute():
            path = ROOT / "tools/maa-resources" / path
        paths.append(path)
    return paths


def _run_native_chain(names: list[str], *, overlay=None, timeout: float = 60,
                      tasks: dict | None = None, capture_path: Path | None = None,
                      maa_dir=None, adb=None, serial=None, maa_user=None) -> dict:
    if not names or any(not isinstance(name, str) or not name for name in names):
        raise ValueError("Custom 链名必须是非空字符串列表")
    if not 0 < timeout < float("inf"):
        raise ValueError("timeout 必须是有限正数")
    outdir = ROOT / "data/captures" / f"native-{datetime.now():%Y%m%d-%H%M%S-%f}"
    outdir.mkdir(parents=True)
    runtime_overlay = None
    if tasks is not None:
        runtime_overlay = outdir / "runtime-overlay"
        resource = runtime_overlay / "resource"
        resource.mkdir(parents=True)
        (resource / "tasks.json").write_text(
            json.dumps(tasks, ensure_ascii=False, indent=2), encoding="utf-8")
    resource_overlay = _overlay_paths(overlay)
    events_path = outdir / "maa-events.jsonl"
    state = {"marks": [], "nodes": [], "results": {}, "failed": False,
             "completed": False, "error": None}
    lock = threading.Lock()
    with events_path.open("w", encoding="utf-8") as events:
        def callback(message, details, _arg):
            with lock:
                try:
                    text = details.decode("utf-8") if details else "{}"
                    message = int(message)
                    events.write(json.dumps({"message": message, "details": text},
                                            ensure_ascii=False) + "\n")
                    events.flush()
                    parsed = json.loads(text)
                    if message in (0, 1):
                        state["error"] = RuntimeError(f"MAA 原生错误：{text}")
                    elif message in (10000, 10004):
                        state["failed"] = True
                    elif message == 10002:
                        state["completed"] = True
                    elif message in (20001, 20002):
                        node = parsed.get("details", {}).get("task", "")
                        if node:
                            state["nodes"].append(node)
                            if message == 20002:
                                state["results"][node] = parsed["details"].get("result", {})
                            # Mark*/EndOfPlot 常刚 Start 就被 Stop 终止，不会有 Completed
                            if ((node.startswith("Mark") or node == "EndOfPlot")
                                    and node not in state["marks"]):
                                state["marks"].append(node)
                except (OSError, ValueError, TypeError, AttributeError, KeyError) as error:
                    state["error"] = error

        asst = None
        try:
            asst = _connect(callback, overlay=resource_overlay, runtime_overlay=runtime_overlay,
                            maa_dir=maa_dir, adb=adb, serial=serial, maa_user=maa_user)
            deadline = time.monotonic() + timeout
            if not asst.append_task("Custom", {"task_names": names}):
                raise RuntimeError(f"MAA 无法添加 Custom 链：{names}")
            if not asst.start():
                raise RuntimeError(f"MAA 无法启动 Custom 链：{names}")
            while asst.running():
                with lock:
                    error = state["error"]
                if error is not None:
                    raise RuntimeError(f"MAA 事件处理失败；日志：{events_path}") from error
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"MAA Custom 链超时；日志：{events_path}")
                time.sleep(0.1)
            with lock:
                if state["error"] is not None:
                    raise RuntimeError(f"MAA 事件处理失败；日志：{events_path}") from state["error"]
                result = {"ok": state["completed"] and not state["failed"],
                          "marks": list(state["marks"]), "nodes": list(state["nodes"]),
                          "results": dict(state["results"]),
                          "events_path": str(events_path.resolve())}
            if capture_path is not None:
                if not result["ok"] or "FreshScreenshot" not in result["nodes"]:
                    raise RuntimeError(f"MAA 未完成新鲜截图；日志：{events_path}")
                image = asst.get_image(1920 * 1080 * 3)
                if not image or image[:8] != b"\x89PNG\r\n\x1a\n":
                    raise ValueError("MAA 新鲜截图为空或不是 PNG")
                # PNG 已由 MAA 编码为 RGB；只读取 IHDR，不重采样或翻转通道。
                width = int.from_bytes(image[16:20], "big")
                height = int.from_bytes(image[20:24], "big")
                if (width, height) != (1280, 720):
                    raise ValueError(f"MAA 截图尺寸不是 1280×720：{width}×{height}")
                capture_path.parent.mkdir(parents=True, exist_ok=True)
                size = capture_path.write_bytes(image)
                result["capture"] = {"path": str(capture_path.resolve()), "width": width,
                                     "height": height, "bytes": size}
            return result
        finally:
            if asst is not None:
                try:
                    if not asst.stop():
                        raise RuntimeError(f"MAA Custom 实例停止失败；日志：{events_path}")
                finally:
                    del asst


def run_native_chain(names: list[str], *, overlay=None,
                     timeout: float = 60, **kw) -> dict:
    """加载基础资源及指定 overlay，执行 Custom 链并返回完成节点、Mark 和事件路径。"""
    return _run_native_chain(names, overlay=overlay, timeout=timeout, **kw)


def skip_story(*, phase: str = "pre", maa_dir=None, adb=None, serial=None,
               maa_user=None, timeout: float = 45) -> dict:
    """原生跳过战前/战后剧情；只有 EndOfPlot 能证明剧情已结束。"""
    if phase not in {"pre", "post"}:
        raise ValueError("phase 必须是 pre 或 post")
    result = run_native_chain(["StorySkipPre" if phase == "pre" else "StorySkipPost"],
                              overlay="story-skip", timeout=timeout, maa_dir=maa_dir,
                              adb=adb, serial=serial, maa_user=maa_user)
    if not result["ok"] or "EndOfPlot" not in result["marks"]:
        raise StorySkipError(f"剧情链未确认 EndOfPlot；日志：{result['events_path']}")
    clicked = any(node in result["nodes"] for node in (
        "SkipThePreBattlePlot", "SkipThePreBattlePlotConfirm", "SkipBattlePlot", "SkipBattlePlotConfirm"))
    payload = {"skipped": clicked, "marks": result["marks"],
               "events_path": result["events_path"]}
    if not clicked:
        payload["reason"] = "no_story"
    return payload


def _ocr_story_advance(nodes, **kw) -> list[str]:
    """原生模板没点掉时用已有 VisionText 兜底：先「跳过」，再「确认」。

    ``tap_text`` 只在文字命中时点击、未命中抛 LookupError，因此这里不会在
    别的画面上盲点（暂停按钮的文字是「暂停」）。返回真正点到的文字。
    """
    clicked = []
    steps = []
    if "SkipBattlePlot" not in nodes:
        steps.append(STORY_SKIP_TEXTS)
    if "SkipBattlePlotConfirm" not in nodes:
        steps.append(STORY_CONFIRM_TEXTS)
    for texts in steps:
        for text in texts:
            try:
                tap_text(text, **kw)
            except LookupError:
                continue
            clicked.append(text)
            break
    return clicked


def advance_post_battle_story(*, timeout: float = 60, max_attempts: int = 2,
                              **kw) -> dict:
    """战后剧情推进：原生跳过 + 确认，再用画面探测证明已经离开剧情页。

    先只读探测：不是剧情页就返回 ``no_story``，一次也不点。是剧情页才执行
    story-skip overlay 的原生链（复用 SkipBattlePlot / SkipBattlePlotConfirm
    模板点击右上「跳过」和原生确认控件）；原生模板没命中时退化到已有
    VisionText 按文字点击。每轮之后都重新探测画面，只有探测确认画面不再是
    剧情页才算 ``advanced``：

    - ``still_story``：剧情页仍在，点击没有效果；
    - ``screen_unknown``：探测没有结论，不能宣称剧情结束。

    ``EndOfPlot`` 只写进 ``end_of_plot`` 供复核，绝不单独作为成功依据。
    """
    if not 0 < timeout < float("inf"):
        raise ValueError("timeout 必须是有限正数")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts 必须是正整数")
    probe = _probe_screen(**kw)
    before = _screen_of(probe["marks"])
    marks = list(probe["marks"])
    events_path = probe["events_path"]
    verified = list(probe["marks"])
    payload = {"advanced": False, "before": before, "after": before, "attempts": 0,
               "clicked": False, "ocr": [], "end_of_plot": False,
               "marks": marks, "events_path": events_path}
    if before != "story":
        # 没有剧情页证据：不点击，明确告知调用方这次没有剧情要跳过。
        payload["reason"] = "no_story" if _left_story(marks) else "screen_unknown"
        return payload
    for attempt in range(1, max_attempts + 1):
        payload["attempts"] = attempt
        chain = run_native_chain(["StorySkipPost"], overlay=STORY_SKIP_OVERLAY,
                                 timeout=timeout, **kw)
        marks.extend(chain["marks"])
        events_path = chain["events_path"]
        payload["end_of_plot"] = payload["end_of_plot"] or "EndOfPlot" in chain["marks"]
        native_clicked = any(node in chain["nodes"] for node in STORY_CLICK_NODES)
        payload["clicked"] = payload["clicked"] or native_clicked
        if not native_clicked or "SkipBattlePlotConfirm" not in chain["nodes"]:
            ocr = _ocr_story_advance(chain["nodes"], **kw)
            payload["ocr"].extend(ocr)
            payload["clicked"] = payload["clicked"] or bool(ocr)
        verify = _probe_screen(**kw)
        marks.extend(verify["marks"])
        events_path = verify["events_path"]
        verified = list(verify["marks"])
        if _left_story(verified):
            payload.update(advanced=True, reason="advanced", after=_screen_of(verified))
            break
        if _screen_of(verified) != "story":
            # 探测没有给出任何画面结论：再点一遍也只是盲点，不能再宣称结束。
            break
    payload["marks"] = marks
    payload["events_path"] = events_path
    if not payload["advanced"]:
        payload["after"] = _screen_of(verified)
        payload["reason"] = ("still_story" if payload["after"] == "story"
                             else "screen_unknown")
    return payload


def recover_stuck_chain(report: dict, **kw) -> dict:
    """仅恢复有原生剧情证据的失败链一次；不重跑 Copilot，不修改战斗胜负。"""
    if (report.get("story_recovery") or {}).get("attempted"):
        return {"attempted": False}
    if report.get("program_status") not in {"failed", "timeout"}:
        return {"attempted": False}
    events_path = report.get("events_path")
    if not events_path:
        return {"attempted": False}
    candidates = {"SkipThePreBattlePlot", "SkipThePreBattlePlotConfirm", "SkipBattlePlot",
                  "SkipBattlePlotConfirm", "ClickPlotStage"}
    story_seen = False
    ended = False
    with Path(events_path).open(encoding="utf-8") as events:
        for line in events:
            if not line.strip():
                continue
            event = json.loads(line)
            details = event["details"]
            if isinstance(details, str):
                details = json.loads(details)
            nested = details.get("details") or {}
            task = nested.get("task", "").rsplit("@", 1)[-1]
            if event["message"] == 20002 and task == "EndOfPlot":
                ended = True
            names = [task, details.get("pre_task", ""), details.get("cur_task", ""),
                     *(details.get("first") or []), *(nested.get("to_be_recognized") or [])]
            if any(name.rsplit("@", 1)[-1] in candidates for name in names):
                story_seen = True
    if not story_seen or ended:
        return {"attempted": False}
    try:
        recovery = skip_story(phase="pre", **kw)
    except (OSError, RuntimeError, ValueError, ImportError) as exc:
        return {"attempted": True, "needs_retry": True,
                "error": f"{type(exc).__name__}: {exc}"}
    return {"attempted": True, **recovery, "needs_retry": True}


def capture_screen(*, out_path: str | None = None, maa_dir=None, adb=None,
                   serial=None, maa_user=None) -> dict:
    """先执行 FreshScreenshot 再取原生 PNG；只截图、不点击、不触发战斗。"""
    path = Path(out_path) if out_path is not None else (
        ROOT / "data/captures" / f"screen-{datetime.now():%Y%m%d-%H%M%S-%f}.png")
    result = _run_native_chain(["FreshScreenshot"], overlay="vision-bridge",
                               capture_path=path, maa_dir=maa_dir, adb=adb,
                               serial=serial, maa_user=maa_user)
    return result["capture"]


def tap_text(text: str, *, timeout: float = 20, **kw) -> dict:
    """以原生 OCR 精确匹配文字并点击；未命中抛 LookupError，不返回空结果。"""
    if not text.strip():
        raise ValueError("点击文字不能为空")
    tasks = {
        "VisionTextEntry": {"algorithm": "JustReturn", "action": "DoNothing",
                            "next": ["VisionTapText", "VisionTextMiss"]},
        "VisionTextMiss": {"algorithm": "JustReturn", "action": "Stop"},
        "VisionTapText": {"algorithm": "OcrDetect", "text": [text], "fullMatch": True,
                          "roi": [0, 0, 1280, 720], "action": "ClickSelf",
                          "maxTimes": 1, "next": []},
    }
    result = _run_native_chain(["VisionTextEntry"], timeout=timeout, tasks=tasks, **kw)
    hit = result["results"].get("VisionTapText")
    if hit is None:
        raise LookupError(f"未命中文字：{text}；日志：{result['events_path']}")
    if not result["ok"]:
        raise RuntimeError(f"文字点击链失败；日志：{result['events_path']}")
    return {"ok": True, "rect": hit["rect"], "text": hit["text"]}


def tap_point(x: int, y: int, **kw) -> dict:
    """仅供 Vision 定位结果点击纯图标/无文字遮罩；坐标基准固定 1280×720。"""
    if type(x) is not int or type(y) is not int or not (0 <= x < 1280 and 0 <= y < 720):
        raise ValueError("Vision 点击坐标必须是 1280×720 内的整数")
    tasks = {"VisionTapPoint": {"algorithm": "JustReturn", "action": "ClickRect",
                                "specificRect": [x, y, 1, 1], "maxTimes": 1, "next": []}}
    result = _run_native_chain(["VisionTapPoint"], tasks=tasks, **kw)
    if not result["ok"] or "VisionTapPoint" not in result["nodes"]:
        raise RuntimeError(f"Vision 坐标点击失败；日志：{result['events_path']}")
    return {"ok": True, "point": [x, y]}


def _check_stage_panel(stage_code: str, **kw) -> dict:
    ready = run_native_chain(["BattleReadyCheck"], overlay="preflight", **kw)
    if not ready["ok"] or "MarkAtPanel" not in ready["marks"]:
        return {**ready, "ok": False, "panel_text": None}
    # 开始按钮只能证明是某个面板；标题必须另外核对，避免返回错误关卡。
    tasks = {
        "RecoveryTargetCheck": {"algorithm": "JustReturn", "action": "DoNothing",
                                "next": ["RecoveryTargetTitle", "MarkRecoveryWrongPanel"]},
        "RecoveryTargetTitle": {"algorithm": "OcrDetect", "text": [stage_code],
                                "fullMatch": False, "roi": [780, 60, 500, 150],
                                "action": "DoNothing", "next": ["MarkRecoveryTargetPanel"]},
        "MarkRecoveryTargetPanel": {"algorithm": "JustReturn", "action": "Stop"},
        "MarkRecoveryWrongPanel": {"algorithm": "JustReturn", "action": "Stop"},
    }
    title = _run_native_chain(["RecoveryTargetCheck"], tasks=tasks, **kw)
    hit = title["results"].get("RecoveryTargetTitle", {})
    text = hit.get("text")
    target = isinstance(text, str) and re.search(re.escape(stage_code) + r"(?![A-Za-z0-9_-])", text, re.I)
    return {**title, "ok": bool(title["ok"] and "MarkRecoveryTargetPanel" in title["marks"] and target),
            "marks": ready["marks"] + title["marks"], "panel_text": text}


def ensure_stage_panel(stage_code: str, *, max_attempts: int = 4, **kw) -> dict:
    """从结算、剧情、地图或详情页归位；每轮截图，只接受原生面板和标题双重确认。"""
    stage_code = stage_code.strip()
    if not stage_code:
        raise ValueError("关卡代号不能为空")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ValueError("max_attempts 必须是正整数")
    marks = []
    events_path = ""
    for attempt in range(1, max_attempts + 1):
        screenshot = capture_screen(**kw)
        panel = _check_stage_panel(stage_code, **kw)
        marks.extend(panel["marks"])
        events_path = panel["events_path"]
        if panel["ok"]:
            return {"ok": True, "attempts": attempt, "panel_text": panel["panel_text"],
                    "marks": marks, "events_path": events_path}
        screen = run_native_chain(["RecoveryScreenCheck"], overlay="probe-and-nav", **kw)
        if not screen["ok"]:
            raise RuntimeError(f"归位画面探测失败；截图：{screenshot['path']}；日志：{screen['events_path']}")
        marks.extend(screen["marks"])
        if "MarkRecoveryResult" in screen["marks"]:
            tap_point(640, 360, **kw)
            screen = run_native_chain(["RecoveryScreenCheck"], overlay="probe-and-nav", **kw)
            if not screen["ok"]:
                raise RuntimeError(f"结算后画面探测失败；截图：{screenshot['path']}；日志：{screen['events_path']}")
            marks.extend(screen["marks"])
        if "MarkRecoveryStory" in screen["marks"]:
            try:
                story = skip_story(phase="post", **kw)
            except StorySkipError:
                try:
                    story = skip_story(phase="pre", **kw)
                except StorySkipError as error:
                    screenshot = capture_screen(**kw)
                    raise RuntimeError(f"剧情归位失败；最后截图：{screenshot['path']}") from error
            marks.extend(story["marks"])
        try:
            tap_text(stage_code, **kw)
        except LookupError:
            back = run_native_chain(["ReturnButtons"], overlay="probe-and-nav", **kw)
            marks.extend(back["marks"])
            if not back["ok"] or "ReturnButton" not in back["nodes"]:
                tap_point(67, 51, **kw)
            try:
                tap_text(stage_code, **kw)
            except LookupError:
                continue
        panel = _check_stage_panel(stage_code, **kw)
        marks.extend(panel["marks"])
        events_path = panel["events_path"]
        if panel["ok"]:
            return {"ok": True, "attempts": attempt, "panel_text": panel["panel_text"],
                    "marks": marks, "events_path": events_path}
    screenshot = capture_screen(**kw)
    raise RuntimeError(f"{max_attempts} 次归位未确认关卡 {stage_code} 面板；"
                       f"最后截图：{screenshot['path']}；日志：{events_path}")


def _probe_screen(**kw) -> dict:
    """只读画面探测；返回原生链原始结果，由调用方用 marks 自己下结论。"""
    return run_native_chain(["NavScreenProbe"], overlay=NAV_OVERLAY, timeout=25, **kw)


def _screen_of(marks) -> str:
    """原生探测 marks → 画面名；没有任何画面 Mark 就是 unknown。"""
    for mark, screen in SCREEN_MARKS:
        if mark in marks:
            return screen
    return "unknown"


def _left_story(marks) -> bool:
    """原生探测已确认画面不是剧情页：命中任一画面 Mark 或明确的 unknown 终点。

    只认探测自己给出的结论；链没跑完（marks 为空）时返回 False，不能把
    「没探测到」当成「已经离开剧情」。
    """
    return (any(mark in marks for mark, screen in SCREEN_MARKS if screen != "story")
            or SCREEN_UNKNOWN_MARK in marks)


def _difficulty_state(**kw) -> dict:
    """读当前难度：险地用 MAA 原生模板，磨难/标准用文字 OCR；识别不到就是 unknown。"""
    result = _run_native_chain(["EpDifficultyProbe"], overlay=(DIFFICULTY_OVERLAY,),
                               timeout=30, **kw)
    mapping = {"MarkEpDifficultyHazard": "hazard", "MarkEpDifficultyTough": "tough",
               "MarkEpDifficultyStandard": "standard"}
    state = next((mapping[mark] for mark in result["marks"] if mark in mapping), "unknown")
    return {"state": state, "marks": result["marks"], "events_path": result["events_path"]}


