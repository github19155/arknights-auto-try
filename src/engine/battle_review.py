"""按指定时间从 battle.mp4 抽帧，并附带事件对齐信息供图片复核。

时间基准
    battle：相对战斗开始。锚点取 ``review-manifest.json`` 的 ``battle_start``
            （battle.mp4 视频秒）；无清单时录像 0 秒即战斗开始。
    leak：相对首次扣血。锚点取清单的 ``leak_time``（视频秒）或
          ``leak_battle_s``（战斗秒）；没有锚点直接 ValueError。
    清单中的 ``battle_start`` / ``leak_time`` 都按视频秒解释，可放在顶层或
    ``anchors`` 子对象里；``leak_battle_s`` 是唯一按战斗秒解释的字段。

抽帧规则
    ``end_s is None`` 表示单点查询；请求时刻不落在帧上时取最近帧，并用
    ``request_delta_s`` 报告实际偏差，绝不用首尾帧冒充目标时刻。
    ``interval_s is None`` 返回时间窗内全部视频帧，否则按间隔抽帧并包含两端点。
    同一帧被重复命中只保留一次。

单次请求上限（硬限制，超过即 ValueError，不自动删帧）
    任意请求最多返回 ``MAX_FRAMES_PER_REQUEST = 60`` 张 PNG；
    ``interval_s=None`` 的逐帧模式还要求时间窗不超过
    ``FULL_FRAME_MAX_WINDOW_S = 2.0`` 秒（30fps 约 60 帧，实际仍受 60 张硬上限约束）。
    默认 ``interval_s=0.5`` 只做稀疏预览；需要更密或更长的窗口时，由调用方显式
    缩小时间窗或调整 ``interval_s``。

帧时间换算
    ``actual_s`` 一律取容器真实呈现时间戳（顺序解码后读 ``CAP_PROP_POS_MSEC``），
    不做 ``帧号 / 平均 FPS`` 换算：单点与抽样端点按真实 PTS 取最近帧，
    ``interval_s=None`` 按真实 PTS 过滤窗口，``battle_s`` / ``leak_relative_s``
    也由真实 PTS 推导。因此 adb screenrecord 之类的 VFR 录像不会出现时间戳
    与图片错位。若封装完全不提供时间戳（``POS_MSEC`` 不可用），退回
    ``帧号 / 平均 FPS`` 估算并在 ``warnings`` 中显式说明。

事件对齐
    ``maa-events.jsonl`` 的逐行结构与 ``src.engine.recorder.parse_maa_events``
    一致：``{"message", "details"}``，``details.what == "CopilotAction"``。
    动作时间取自 ``details.details.elapsed_time``（MAA 语义为战斗已进行秒数）；
    ``-1`` 表示没有时间戳，这类动作不参与最近动作匹配。最近动作按战斗秒
    距离取绝对值最小者，返回 ``{action, action_index, battle_s, delta_s}``。
"""
from __future__ import annotations

import json
import math
import shutil
from datetime import datetime
from itertools import chain
from pathlib import Path

import cv2

MANIFEST_NAME = "review-manifest.json"
VIDEO_NAME = "battle.mp4"
EVENTS_NAME = "maa-events.jsonl"

MAX_FRAMES_PER_REQUEST = 60
FULL_FRAME_MAX_WINDOW_S = 2.0
MAX_SAMPLE_TARGETS = 10_000  # 间隔极小时的候选点保护上限，去重后再按张数判限
SEEK_MARGIN_S = 2.0  # 按目标时间 seek 时预留的前置余量，避免关键帧落在目标之后

TIME_BASES = {"battle", "leak"}
_EPS = 1e-9
_ROUND = 3


def extract_review_frames(
    run_dir: str,
    start_s: float,
    end_s: float | None = None,
    interval_s: float | None = 0.5,
    time_base: str = "battle",
) -> dict:
    """从 run_dir/battle.mp4 抽取复核帧，PNG 写入 run_dir/review_frames/<唯一子目录>/。

    参数与时间基准见模块文档字符串；单次最多 60 张，逐帧模式窗口 ≤ 2.0 秒。
    返回 ``{ok, run_dir, time_base, requested, frames, frame_dir, warnings}``，
    每个 frame 含 ``frame_path / actual_s / requested_s / request_delta_s /
    battle_s / leak_relative_s / nearest_action``。

    Raises:
        ValueError: 参数非法、请求超出录像范围、leak 无锚点、帧数超上限、
            清单或事件文件结构错误、缺少 run_dir/battle.mp4。
        RuntimeError: 录像打不开、FPS/帧数非法、单帧读取或 PNG 写入失败。
    """
    if time_base not in TIME_BASES:
        raise ValueError(f"time_base 必须是 {sorted(TIME_BASES)} 之一：{time_base!r}")
    start = _as_float(start_s, "start_s")
    end = start if end_s is None else _as_float(end_s, "end_s")
    if end < start:
        raise ValueError(f"start_s({start}) 不能大于 end_s({end})")
    interval = None if interval_s is None else _as_float(interval_s, "interval_s")
    if interval is not None and interval <= 0:
        raise ValueError(f"interval_s 必须为正数或 None：{interval}")

    root = Path(run_dir)
    if not root.is_dir():
        raise ValueError(f"run_dir 不是目录：{root}")
    video = root / VIDEO_NAME
    if not video.is_file():
        raise ValueError(f"run_dir 缺少 {VIDEO_NAME}：{root}")

    warnings: list[str] = []
    battle_start, leak_video_s = _load_anchors(root, warnings)
    origin = battle_start if time_base == "battle" else leak_video_s
    if origin is None:
        raise ValueError(
            f"{MANIFEST_NAME} 没有 leak 时间锚点（leak_time / leak_battle_s），"
            "无法使用 leak 时间基准"
        )

    video_start = origin + start
    video_end = origin + end
    fps, frame_count = _video_geometry(video)
    duration_s = frame_count / fps
    for label, value in (("start_s", video_start), ("end_s", video_end)):
        if value < -_EPS or value > duration_s + _EPS:
            raise ValueError(
                f"{label} 换算到视频时间 {value:.3f}s 超出录像范围 [0, {duration_s:.3f}]"
            )

    if interval is None:
        if video_end - video_start > FULL_FRAME_MAX_WINDOW_S + _EPS:
            raise ValueError(
                f"interval_s=None 仅允许 {FULL_FRAME_MAX_WINDOW_S}s 内的小窗口逐帧；"
                f"当前窗口 {video_end - video_start:.3f}s，请缩小时间窗或指定 interval_s"
            )
        targets: list[float] | None = None
        window: tuple[float, float] | None = (video_start, video_end)
    else:
        steps = math.floor((video_end - video_start) / interval + _EPS)
        estimated = steps + 2
        if estimated > MAX_SAMPLE_TARGETS:
            raise ValueError(
                f"interval_s={interval} 过小，抽样点约 {estimated} 个，"
                f"超过候选上限 {MAX_SAMPLE_TARGETS}；请增大 interval_s"
            )
        targets = [video_start + i * interval for i in range(steps + 1)]
        if video_end - targets[-1] > _EPS:
            targets.append(video_end)
        window = None

    actions, action_warnings = _load_actions(root)
    warnings.extend(action_warnings)

    frame_dir = _make_frame_dir(root, time_base, start, end)
    try:
        picks = _select_frames(video, frame_dir, fps=fps, targets=targets,
                               window=window, warnings=warnings)
    except BaseException:
        shutil.rmtree(frame_dir, ignore_errors=True)
        raise

    frames = []
    for pick in picks:
        actual_s = pick["video_s"]
        battle_s = actual_s - battle_start
        leak_relative_s = None if leak_video_s is None else actual_s - leak_video_s
        frames.append({
            "frame_path": str(pick["path"]),
            "actual_s": round(actual_s, _ROUND),
            "requested_s": round(pick["requested_video_s"] - origin, _ROUND),
            "request_delta_s": round(actual_s - pick["requested_video_s"], 6),
            "battle_s": round(battle_s, _ROUND),
            "leak_relative_s": (None if leak_relative_s is None
                                else round(leak_relative_s, _ROUND)),
            "nearest_action": _nearest_action(actions, battle_s),
        })

    return {
        "ok": True,
        "run_dir": str(root),
        "time_base": time_base,
        "requested": {
            "start_s": start,
            "end_s": None if end_s is None else end,
            "interval_s": interval,
            "time_base": time_base,
            "video_start_s": round(video_start, _ROUND),
            "video_end_s": round(video_end, _ROUND),
        },
        "frames": frames,
        "frame_dir": str(frame_dir),
        "warnings": warnings,
    }


def _as_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数字：{value!r}")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} 必须是有限数字：{value!r}")
    return result


def _clamp_time(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _load_anchors(root: Path, warnings: list[str]) -> tuple[float, float | None]:
    """读取清单锚点；无清单时 battle 以视频 0 秒开始，leak 保持 None。"""
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        warnings.append(
            f"{MANIFEST_NAME} 缺失：battle 以录像 0 秒为起点；leak 无锚点不可用"
        )
        return 0.0, None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{manifest} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{manifest} 顶层必须是 JSON 对象")

    battle_start = _dig_anchor(data, ("battle_start", "battle_start_s",
                                      "battle_start_video_s"), "battle_start")
    if battle_start is None:
        warnings.append(f"{MANIFEST_NAME} 缺少 battle_start：battle 以录像 0 秒为起点")
        battle_start = 0.0
    leak_video_s = _dig_anchor(data, ("leak_video_s", "leak_time", "leak_time_s"), "leak_time")
    if leak_video_s is None:
        leak_battle_s = _dig_anchor(data, ("leak_battle_s",), "leak_battle_s")
        if leak_battle_s is not None:
            leak_video_s = battle_start + leak_battle_s
    if battle_start < 0:
        raise ValueError(f"{MANIFEST_NAME} 的 battle_start 不能为负数：{battle_start}")
    return battle_start, leak_video_s


def _dig_anchor(data: dict, keys: tuple[str, ...], label: str) -> float | None:
    containers = [data]
    nested = data.get("anchors")
    if isinstance(nested, dict):
        containers.append(nested)
    for container in containers:
        for key in keys:
            if key not in container:
                continue
            value = container[key]
            if isinstance(value, dict):
                for inner in ("video_s", "s", "seconds", "time_s"):
                    if inner in value:
                        return _as_float(value[inner], f"{MANIFEST_NAME}.{label}.{inner}")
                raise ValueError(f"{MANIFEST_NAME}.{label} 缺少数值时间字段")
            return _as_float(value, f"{MANIFEST_NAME}.{label}")
    return None


def _video_geometry(video: Path) -> tuple[float, int]:
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"录像不可读：{video}")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS))
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()
    if not math.isfinite(fps) or fps <= 0:
        raise RuntimeError(f"录像 FPS 非法：{fps!r}（{video}）")
    if frame_count <= 0:
        raise RuntimeError(f"录像帧数非法：{frame_count}（{video}）")
    return fps, frame_count


def _select_frames(video: Path, frame_dir: Path, *, fps: float,
                   targets: list[float] | None,
                   window: tuple[float, float] | None,
                   warnings: list[str]) -> list[dict]:
    """顺序解码，按真实 PTS 选帧并落盘。

    ``targets`` 非空时按每个目标时间取真实 PTS 最近帧；``window`` 非空时
    （全部帧模式）只保留真实 PTS 落在窗口内的帧。返回
    ``[{video_s, requested_video_s, path}]``，顺序与目标/解码顺序一致。
    """
    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"录像不可读：{video}")
    try:
        use_pts = _has_container_pts(cap)
        if not use_pts:
            warnings.append(
                "录像未提供可用的 CAP_PROP_POS_MSEC 时间戳：actual_s 退回 "
                "帧号 / 平均 FPS 估算，VFR 素材不保证准确"
            )
        first_target = targets[0] if targets is not None else window[0]
        # 没有可信 PTS 时按帧号算时间，seek 会引入帧号/时间错位，故从头解码。
        seek_s = max(0.0, first_target - SEEK_MARGIN_S) if use_pts else 0.0
        _seek(cap, seek_s, use_pts)
        frames = _iter_frames(cap, fps, use_pts)
        first = next(frames, None)
        if (first is not None and use_pts and seek_s > 0.0
                and first[1] > first_target + _EPS):
            # 个别后端按最近关键帧（而非向前）seek，落到目标之后时从头重扫。
            _seek(cap, 0.0, use_pts)
            frames = _iter_frames(cap, fps, use_pts)
            first = next(frames, None)
        if first is None:
            raise RuntimeError(f"无法从录像读取任何帧：{video}")
        return _choose_frames(chain((first,), frames), frame_dir, targets, window)
    finally:
        cap.release()


def _has_container_pts(cap) -> bool:
    """读前两帧确认封装是否给出递增的时间戳；读完后调用方须重新 seek。"""
    times: list[float] = []
    for _ in range(2):
        ok, _image = cap.read()
        if not ok:
            break
        times.append(float(cap.get(cv2.CAP_PROP_POS_MSEC)))
    if len(times) < 2:
        return False
    if not all(math.isfinite(t) for t in times):
        return False
    return times[1] > times[0]


def _seek(cap, seconds: float, use_pts: bool) -> None:
    if use_pts:
        cap.set(cv2.CAP_PROP_POS_MSEC, max(seconds, 0.0) * 1000.0)
    else:
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)


def _iter_frames(cap, fps: float, use_pts: bool):
    """顺序解码，产出 ``(解码序号, 绝对时间秒, 图像)``。"""
    index = 0
    previous = -math.inf
    while True:
        ok, image = cap.read()
        if not ok:
            return
        if use_pts:
            seconds = float(cap.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
            if not math.isfinite(seconds):
                raise RuntimeError(f"录像第 {index} 帧时间戳非法：{seconds!r}")
        else:
            seconds = index / fps
        if seconds < previous - _EPS:
            raise RuntimeError(
                f"录像时间戳不单调：第 {index} 帧 {seconds:.6f}s 早于前一帧 {previous:.6f}s"
            )
        previous = seconds
        yield index, seconds, image
        index += 1


def _choose_frames(frames, frame_dir: Path,
                   targets: list[float] | None,
                   window: tuple[float, float] | None) -> list[dict]:
    picks: list[dict] = []
    seen: set[int] = set()
    previous: tuple[int, float, object] | None = None
    position = 0

    def emit(index: int, seconds: float, image, requested: float) -> None:
        if index in seen:
            return
        seen.add(index)
        dest = frame_dir / f"{seconds:010.3f}s.png"
        suffix = 1
        while dest.exists():
            suffix += 1
            dest = frame_dir / f"{seconds:010.3f}s-{suffix}.png"
        if not cv2.imwrite(str(dest), image):
            raise RuntimeError(f"PNG 写入失败：{dest}")
        picks.append({"video_s": seconds, "requested_video_s": requested,
                      "path": dest})
        if len(picks) > MAX_FRAMES_PER_REQUEST:
            raise ValueError(
                f"请求将返回超过 {MAX_FRAMES_PER_REQUEST} 张图"
                f"（{'全部帧' if targets is None else 'interval 抽样'}），"
                f"超过单次上限 {MAX_FRAMES_PER_REQUEST}；"
                "请缩小时间窗或增大 interval_s"
            )

    for index, seconds, image in frames:
        if targets is None:
            low, high = window
            if low - _EPS <= seconds <= high + _EPS:
                emit(index, seconds, image, _clamp_time(seconds, low, high))
        else:
            while position < len(targets):
                target = targets[position]
                if previous is not None and previous[1] <= target < seconds:
                    if target - previous[1] <= seconds - target:
                        emit(previous[0], previous[1], previous[2], target)
                    else:
                        emit(index, seconds, image, target)
                    position += 1
                elif previous is None and seconds >= target:
                    emit(index, seconds, image, target)
                    position += 1
                else:
                    break
            if position >= len(targets):
                break
        previous = (index, seconds, image)

    if targets is not None and previous is not None:
        # 目标超出最后一帧 PTS 时取末帧（与旧的边界钳制语义一致）。
        while position < len(targets):
            emit(previous[0], previous[1], previous[2], targets[position])
            position += 1
    if not picks:
        raise ValueError("请求时间窗内没有可用帧")
    return picks


def _load_actions(root: Path) -> tuple[list[dict], list[str]]:
    """按 parse_maa_events 的逐行结构解析带时间戳的 CopilotAction。"""
    path = root / EVENTS_NAME
    warnings: list[str] = []
    if not path.is_file():
        warnings.append(f"{EVENTS_NAME} 缺失：nearest_action 为空")
        return [], warnings

    actions: list[dict] = []
    action_count = 0
    untimed = 0
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            raw_details = event["details"]
            details = json.loads(raw_details) if isinstance(raw_details, str) else raw_details
            int(event["message"])
            if not isinstance(details, dict):
                raise ValueError("details 必须是 JSON 对象")
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{path}:{line_number}: MAA 事件格式错误：{exc}") from exc
        if details.get("what") != "CopilotAction":
            continue
        nested = details.get("details") or {}
        if not isinstance(nested, dict):
            nested = {}
        action_count += 1
        action = nested.get("action", "")
        target = nested.get("target") or nested.get("name") or ""
        label = " ".join(str(part) for part in (action, target) if part) or None
        location = nested.get("location")
        if location is not None and label:
            if isinstance(location, dict):
                location = [location["x"], location["y"]]
            label += f" @ ({location[0]},{location[1]})"
        elapsed = nested.get("elapsed_time")
        if isinstance(elapsed, bool) or not isinstance(elapsed, (int, float)) \
                or not math.isfinite(float(elapsed)) or elapsed < 0:
            untimed += 1
            continue
        actions.append({
            "label": label,
            "action_index": nested.get("action_index", nested.get("index", action_count - 1)),
            "battle_s": float(elapsed),
        })

    if untimed:
        warnings.append(
            f"{untimed} 个 CopilotAction 缺少 elapsed_time，未参与最近动作匹配"
        )
    if not actions:
        warnings.append(f"{EVENTS_NAME} 没有带时间戳的 CopilotAction：nearest_action 为空")
    return actions, warnings


def _nearest_action(actions: list[dict], battle_s: float) -> dict | None:
    if not actions:
        return None
    best = min(actions, key=lambda item: abs(item["battle_s"] - battle_s))
    return {
        "action": best["label"],
        "action_index": best["action_index"],
        "battle_s": round(best["battle_s"], _ROUND),
        "delta_s": round(battle_s - best["battle_s"], _ROUND),
    }


def _make_frame_dir(root: Path, time_base: str, start: float, end: float) -> Path:
    base = root / "review_frames"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    name = f"{stamp}-{time_base}-{start:g}_{end:g}"
    candidate = base / name
    suffix = 1
    while True:
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            suffix += 1
            candidate = base / f"{name}-{suffix}"

