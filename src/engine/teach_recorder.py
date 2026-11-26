#!/usr/bin/env python3
"""触控示教器：只读监听 `getevent`，把人类操作录成结构化动作流与连环画。

只读约束：只用 `adb shell getevent -lp` / `getevent -lt` 与 `asst.get_image()`，
不注入触摸、不点击、不跑 MAA 任务、不消耗理智。

硬件坐标先按 `getevent -lp` 的 ABS 轴范围线性映射到 1280×720，再用
`tile_projection.pixel_to_tile` 反投影成瓦片；没有 Tile-Pos 视图数据时
`tile`/`tile_end` 保持 `None`，绝不猜测格子。
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from src.engine.recorder import load_projection_meta
from src.engine.runner import _decode_frame
from src.engine.tile_projection import infer_drag_direction, pixel_to_tile

ROOT = Path(__file__).resolve().parents[2]
SCREEN = (1280, 720)
LEARNED_OPS = ROOT / "data" / "learned_ops.md"
DRAG_THRESHOLD_PX = 24  # 小于此位移按点按处理；人手指按下的抖动通常 < 10 px

_EVENT_LINE = re.compile(
    r"^\[\s*(?P<t>\d+\.\d+)\]\s*(?P<device>/dev/input/event\d+):\s*"
    r"(?P<type>[A-Z0-9_]+|[0-9a-fA-F]{4})\s+"
    r"(?P<code>[A-Z0-9_]+|[0-9a-fA-F]{4})\s+"
    r"(?P<value>[0-9a-fA-F]{1,8})"
)
_ADD_DEVICE = re.compile(r"^add device \d+:\s*(?P<path>\S+)")
_DEVICE_NAME = re.compile(r'^\s+name:\s*"(?P<name>[^"]*)"')
# `getevent -lp` 只在组首行打印 `ABS (0003):`，续行直接给代码名。
_GROUP_HEADER = re.compile(
    r"^\s+(?P<group>ABS|KEY)\s+\((?P<group_code>[0-9a-fA-F]+)\):(?P<rest>.*)$"
)
_AXIS_ENTRY = re.compile(
    r"^\s*(?P<code>[A-Z0-9_]+)\s*:\s*value\s+-?\d+,\s*min\s*(?P<min>-?\d+),"
    r"\s*max\s*(?P<max>-?\d+)"
)
_TYPE_NAMES = {"0000": "EV_SYN", "0001": "EV_KEY", "0003": "EV_ABS"}
_CODE_NAMES = {
    "0000": "ABS_X",
    "0001": "ABS_Y",
    "0035": "ABS_MT_POSITION_X",
    "0036": "ABS_MT_POSITION_Y",
    "0039": "ABS_MT_TRACKING_ID",
    "014a": "BTN_TOUCH",
}


def _adb_serial(adb=None, serial=None) -> tuple[str, str]:
    """与 navigator._connect 相同的连接参数来源，避免出现两套默认值。"""
    return (str(adb or os.environ.get("MAA_ADB", "adb")),
            str(serial or os.environ.get("MAA_SERIAL", "127.0.0.1:5555")))


def _hex_value(text: str) -> int:
    """getevent 的值按 32 位无符号十六进制打印；f… 回绕成负数。"""
    raw = int(text, 16)
    return raw - (1 << 32) if raw >= (1 << 31) else raw


def parse_getevent_line(line: str) -> dict | None:
    """解析一行 `getevent -lt`；不是事件行（空行/提示）返回 None。"""
    match = _EVENT_LINE.match(line.strip())
    if match is None:
        return None
    kind = _TYPE_NAMES.get(match.group("type"), match.group("type"))
    code = _CODE_NAMES.get(match.group("code"), match.group("code"))
    return {
        "t": float(match.group("t")),
        "device": match.group("device"),
        "type": kind,
        "code": code,
        "value": _hex_value(match.group("value")),
    }


def parse_getevent_devices(text: str) -> list[dict]:
    """解析 `getevent -lp`，返回设备及其 ABS/KEY 轴范围。"""
    devices: list[dict] = []
    current: dict | None = None
    group: str | None = None
    for line in text.splitlines():
        added = _ADD_DEVICE.match(line)
        if added is not None:
            current = {"path": added.group("path"), "name": "", "axes": {}, "keys": {}}
            devices.append(current)
            group = None
            continue
        if current is None:
            continue
        named = _DEVICE_NAME.match(line)
        if named is not None:
            current["name"] = named.group("name")
            continue
        header = _GROUP_HEADER.match(line)
        rest = line
        if header is not None:
            group = header.group("group")
            rest = header.group("rest")
        entry = _AXIS_ENTRY.match(rest)
        if entry is None or group is None:
            continue
        span = (int(entry.group("min")), int(entry.group("max")))
        table = current["axes"] if group == "ABS" else current["keys"]
        table[entry.group("code")] = span
    return devices


def pick_touch_device(devices: list[dict]) -> dict:
    """挑出上报触摸坐标的设备；多指轴优先于单指轴。无可用设备时抛 RuntimeError。"""
    for x_code, y_code in (("ABS_MT_POSITION_X", "ABS_MT_POSITION_Y"), ("ABS_X", "ABS_Y")):
        for device in devices:
            if x_code in device["axes"] and y_code in device["axes"]:
                return {
                    "path": device["path"],
                    "name": device["name"],
                    "x_code": x_code,
                    "y_code": y_code,
                    "x_range": device["axes"][x_code],
                    "y_range": device["axes"][y_code],
                    "multitouch": x_code == "ABS_MT_POSITION_X",
                }
    raise RuntimeError("getevent -lp 中没有找到触摸 ABS 轴，无法映射坐标")


def map_hw_to_screen(x: float, y: float, x_range: tuple[int, int], y_range: tuple[int, int],
                     resolution: tuple[int, int] = SCREEN,
                     *, swap_axes: bool | None = None) -> tuple[int, int]:
    """把硬件 ABS 坐标线性映射到基准分辨率；设备/画面长短轴不一致时自动换轴。"""
    width, height = resolution
    if width <= 0 or height <= 0:
        raise ValueError("目标分辨率必须为正数")
    min_x, max_x = int(x_range[0]), int(x_range[1])
    min_y, max_y = int(y_range[0]), int(y_range[1])
    if max_x <= min_x or max_y <= min_y:
        raise ValueError(f"ABS 轴范围必须递增：x={x_range} y={y_range}")
    u = min(max((float(x) - min_x) / (max_x - min_x), 0.0), 1.0)
    v = min(max((float(y) - min_y) / (max_y - min_y), 0.0), 1.0)
    if swap_axes is None:
        hw_landscape = (max_x - min_x) >= (max_y - min_y)
        swap_axes = hw_landscape != (width >= height)
    if swap_axes:
        u, v = v, u
    return (int(round(u * (width - 1))), int(round(v * (height - 1))))


class GestureDetector:
    """把 `getevent -lt` 事件流增量解析成按下/抬起手势；实时喂行即可抓关键帧。"""

    def __init__(self, *, device: str | None = None, x_range: tuple[int, int],
                 y_range: tuple[int, int], resolution: tuple[int, int] = SCREEN,
                 swap_axes: bool | None = None,
                 drag_threshold: float = DRAG_THRESHOLD_PX):
        if drag_threshold < 0:
            raise ValueError("拖拽阈值不能为负")
        self.device = device
        self.x_range = tuple(x_range)
        self.y_range = tuple(y_range)
        self.resolution = resolution
        self.swap_axes = swap_axes
        self.drag_threshold = drag_threshold
        self.gestures: list[dict] = []
        self._raw: tuple[int | None, int | None] = (None, None)  # 两轴到齐才算坐标
        self._screen: tuple[int, int] | None = None
        # 抬指后每根轴都残留旧值；按下点只在两根轴都重新上报后才可信。
        self._fresh = [False, False]
        self._active: dict | None = None
        self._index = 0
        self._last_t = 0.0

    def _set_axis(self, axis: int, value: int) -> None:
        self._fresh[axis] = True
        self._raw = (value, self._raw[1]) if axis == 0 else (self._raw[0], value)
        if self._raw[0] is None or self._raw[1] is None:
            return  # 单轴事件不能当坐标用
        self._screen = map_hw_to_screen(self._raw[0], self._raw[1], self.x_range,
                                        self.y_range, self.resolution,
                                        swap_axes=self.swap_axes)
        if self._active is not None and self._active["down_screen"] is None and all(self._fresh):
            # BTN_TOUCH 先于坐标到达：第一组完整坐标就是按下点。
            self._active["down_raw"] = self._raw
            self._active["down_screen"] = self._screen

    def _begin(self, t: float) -> list[dict]:
        if self._active is not None:
            return []  # 多指只跟随第一根手指；示教不需要双指
        complete = all(self._fresh) and self._raw[0] is not None and self._raw[1] is not None
        down_raw = self._raw if complete else None
        down_screen = self._screen if complete else None
        self._fresh = [False, False]  # 之后的坐标才算新姿态
        self._active = {"index": self._index, "down_t": t, "down_raw": down_raw,
                        "down_screen": down_screen, "last_t": t}
        return [{"kind": "down", "index": self._index, "t": t,
                 "raw": down_raw, "screen": down_screen}]

    def _end(self, t: float) -> list[dict]:
        active = self._active
        self._active = None
        if active is None:
            return []
        if self._screen is None or self._raw[0] is None or self._raw[1] is None:
            return []  # 没有坐标的按键事件不能作为触控动作
        # 单轴设备可能从不重发未变化的轴：此时退回抬起点，按点按记录。
        down_raw = active["down_raw"] if active["down_raw"] is not None else self._raw
        down_screen = active["down_screen"] if active["down_screen"] is not None else self._screen
        up_screen = self._screen
        move = ((up_screen[0] - down_screen[0]) ** 2 + (up_screen[1] - down_screen[1]) ** 2) ** 0.5
        kind = "drag" if move >= self.drag_threshold else "tap"
        gesture = {
            "index": active["index"],
            "kind": kind,
            "down_t": active["down_t"],
            "up_t": t,
            "duration": t - active["down_t"],
            "move_px": move,
            "raw": {"down": down_raw, "up": self._raw},
            "screen": {"down": down_screen, "up": up_screen},
            "direction": (infer_drag_direction(down_screen[0], down_screen[1],
                                               up_screen[0], up_screen[1])
                          if kind == "drag" else None),
            "truncated": False,
        }
        self.gestures.append(gesture)
        self._index += 1
        self._fresh = [False, False]
        return [{"kind": "up", "index": gesture["index"], "t": t,
                 "raw": self._raw, "screen": self._screen}]

    def feed(self, line: str) -> list[dict]:
        """吃一行原始输出，返回本次新增的 down/up 事件（没有则空列表）。"""
        event = parse_getevent_line(line)
        if event is None:
            return []
        if self.device is not None and event["device"] != self.device:
            return []
        self._last_t = max(self._last_t, event["t"])
        code, value, t = event["code"], event["value"], event["t"]

        if code in ("ABS_MT_POSITION_X", "ABS_X"):
            self._set_axis(0, value)
        elif code in ("ABS_MT_POSITION_Y", "ABS_Y"):
            self._set_axis(1, value)
        elif code == "ABS_MT_TRACKING_ID":
            return self._begin(t) if value >= 0 else self._end(t)
        elif code == "BTN_TOUCH":
            return self._begin(t) if value == 1 else self._end(t)
        return []

    def finish(self) -> None:
        """录制被时长截断时，把未抬起的手势按最后一帧补齐，避免丢动作。"""
        if self._active is None:
            return
        active = self._active
        if active["down_screen"] is not None and self._screen is not None:
            self._end(max(self._last_t, active["down_t"]))
            self.gestures[-1]["truncated"] = True
        self._active = None


def parse_touch_gestures(lines, *, device: str | None = None,
                         x_range: tuple[int, int], y_range: tuple[int, int],
                         resolution: tuple[int, int] = SCREEN,
                         swap_axes: bool | None = None,
                         drag_threshold: float = DRAG_THRESHOLD_PX) -> list[dict]:
    """纯函数：整段 getevent 文本 → 手势列表（按下/抬起坐标与时间戳）。"""
    detector = GestureDetector(device=device, x_range=x_range, y_range=y_range,
                               resolution=resolution, swap_axes=swap_axes,
                               drag_threshold=drag_threshold)
    for line in lines:
        detector.feed(line)
    detector.finish()
    return detector.gestures


def build_actions(gestures: list[dict], *, meta: dict | None = None,
                  resolution: tuple[int, int] = SCREEN,
                  t_base: float | None = None) -> list[dict]:
    """手势 → 结构化动作流：相对时间、屏幕点、反算瓦片与朝向。"""
    base = t_base if t_base is not None else (gestures[0]["down_t"] if gestures else 0.0)
    actions = []
    for gesture in gestures:
        down = gesture["screen"]["down"]
        up = gesture["screen"]["up"]
        tile, tile_end = None, None
        if meta is not None:
            common = {"width": meta["width"], "height": meta["height"],
                      "height_type": meta["height_types"], "view": meta["view"],
                      "screen": resolution}
            tile = pixel_to_tile(down[0], down[1], **common)
            tile_end = pixel_to_tile(up[0], up[1], **common)
        actions.append({
            "t": round(gesture["down_t"] - base, 3),
            "duration": round(gesture["duration"], 3),
            "type": "DRAG" if gesture["kind"] == "drag" else "TAP",
            "screen": list(down),
            "end_screen": list(up),
            "tile": None if tile is None else list(tile),
            "tile_end": None if tile_end is None else list(tile_end),
            "direction": gesture["direction"],
            "truncated": gesture["truncated"],
        })
    return actions


def compose_teach_strip(steps: list[dict], *, out_path, stage_code: str,
                        columns: int = 2, cell_size: tuple[int, int] = (640, 360)) -> Path:
    """关键帧连环画：红准星标触点、步骤序号、拖拽箭头，拼成单张 PNG。

    `steps` 每项含 `frame`（PIL 图）、`point`（屏幕坐标）与可选 `arrow`
    （(x1, y1, x2, y2) 屏幕坐标）。坐标按帧缩放自动换算。
    """
    if not steps:
        raise ValueError("示教连环画至少需要一帧")
    if columns <= 0:
        raise ValueError("列数必须为正")
    cell_w, cell_h = cell_size
    if cell_w <= 0 or cell_h <= 0:
        raise ValueError("单元格尺寸必须为正")
    rows = (len(steps) + columns - 1) // columns
    canvas = Image.new("RGB", (cell_w * columns, cell_h * rows), "#0d1117")
    draw = ImageDraw.Draw(canvas)

    for order, step in enumerate(steps):
        frame = step["frame"]
        if not isinstance(frame, Image.Image):
            raise ValueError("示教步骤的 frame 必须是 PIL 图像")
        frame = frame.convert("RGB")
        left = (order % columns) * cell_w
        top = (order // columns) * cell_h
        picture = ImageOps.contain(frame, (cell_w, cell_h))
        offset_x = left + (cell_w - picture.width) // 2
        offset_y = top + (cell_h - picture.height) // 2
        canvas.paste(picture, (offset_x, offset_y))
        scale = picture.width / frame.width

        def to_cell(point, _ox=offset_x, _oy=offset_y, _s=scale):
            return _ox + point[0] * _s, _oy + point[1] * _s

        if step.get("point") is not None:
            cx, cy = to_cell(step["point"])
            draw.ellipse((cx - 16, cy - 16, cx + 16, cy + 16), outline="#ff3b30", width=3)
            draw.line((cx - 26, cy, cx + 26, cy), fill="#ff3b30", width=2)
            draw.line((cx, cy - 26, cx, cy + 26), fill="#ff3b30", width=2)

        if step.get("arrow") is not None:
            x1, y1, x2, y2 = step["arrow"]
            start = to_cell((x1, y1))
            end = to_cell((x2, y2))
            draw.line((start[0], start[1], end[0], end[1]), fill="#ff9500", width=3)
            head = 14.0
            length = max(((end[0] - start[0]) ** 2 + (end[1] - start[1]) ** 2) ** 0.5, 1e-6)
            ux, uy = (end[0] - start[0]) / length, (end[1] - start[1]) / length
            for angle in (0.5, -0.5):
                cos_a, sin_a = math.cos(angle), math.sin(angle)
                hx = end[0] - head * (ux * cos_a - uy * sin_a)
                hy = end[1] - head * (ux * sin_a + uy * cos_a)
                draw.line((end[0], end[1], hx, hy), fill="#ff9500", width=3)

        label = f"#{step.get('index', order + 1)} {step.get('label', '')}".strip()
        draw.rectangle((left, top, left + 260, top + 22), fill="#161b22")
        draw.text((left + 6, top + 5), f"{stage_code} {label}".strip(), fill="#f0f6fc")
        draw.rectangle((left, top, left + cell_w - 1, top + cell_h - 1), outline="#6e7681")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")
    return out_path


def _list_devices(adb: str, serial: str) -> str:
    result = subprocess.run([adb, "-s", serial, "shell", "getevent", "-lp"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            encoding="utf-8", errors="replace", timeout=20, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"getevent -lp 失败（{result.returncode}）：{result.stderr.strip()}")
    if not result.stdout.strip():
        raise RuntimeError("getevent -lp 没有输出；确认 adb 与设备连接")
    return result.stdout


def _grab_frame(asst, resolution: tuple[int, int]) -> Image.Image:
    """只在按下/抬起瞬间抓一帧；MAA 实机返回 1280×720 PNG。"""
    width, height = resolution
    image = asst.get_image(width * height * 3)
    frame = _decode_frame(image, width, height)
    if frame is None:
        raise RuntimeError("asst.get_image() 返回空帧，无法保存示教关键帧")
    return frame


def _open_capture(maa_dir=None, adb=None, serial=None, maa_user=None):
    """复用 navigator 的连接流程；本模块只调用 get_image，不 append_task。"""
    from src.engine.navigator import _connect

    return _connect(lambda *_args: None, maa_dir=maa_dir, adb=adb, serial=serial,
                    maa_user=maa_user)


def record_human_demo(stage_code: str, duration_s: float, *, outdir=None, adb=None,
                      serial=None, maa_dir=None, maa_user=None,
                      resolution: tuple[int, int] = SCREEN) -> dict:
    """只读录制人类操作：监听 getevent + 抓按下/抬起关键帧 + 反算瓦片。

    返回结构化动作流；`strip_path` 是带红准星/序号/箭头的连环画。全程不注入触摸。
    """
    code = (stage_code or "").strip()
    if not code:
        raise ValueError("关卡代号不能为空")
    if duration_s <= 0:
        raise ValueError("录制时长必须为正数")
    adb, serial = _adb_serial(adb, serial)

    listing = _list_devices(adb, serial)
    device = pick_touch_device(parse_getevent_devices(listing))
    meta = load_projection_meta(code, maa_dir=maa_dir)

    safe_code = re.sub(r"[^A-Za-z0-9_-]", "_", code)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    folder = Path(outdir) if outdir is not None else ROOT / "data/captures" / f"teach-{safe_code}-{stamp}"
    folder.mkdir(parents=True, exist_ok=True)

    asst = None
    proc = None
    raw_lines: list[str] = []
    captures: list[dict] = []
    errors: list[str] = []
    detector = GestureDetector(device=device["path"], x_range=device["x_range"],
                               y_range=device["y_range"], resolution=resolution)
    stop_listening = threading.Event()

    def listen() -> None:
        try:
            for line in proc.stdout:
                raw_lines.append(line.rstrip("\r\n"))
                for event in detector.feed(line):
                    frame = _grab_frame(asst, resolution)
                    captures.append({"index": event["index"], "kind": event["kind"],
                                     "t": event["t"], "screen": event["screen"],
                                     "frame": frame})
                if stop_listening.is_set():
                    break
        except Exception as exc:  # 线程里的异常必须带回主线程，不能静默
            errors.append(f"getevent 监听失败：{type(exc).__name__}: {exc}")
            stop_listening.set()

    try:
        asst = _open_capture(maa_dir=maa_dir, adb=adb, serial=serial, maa_user=maa_user)
        _grab_frame(asst, resolution)  # 预热：先确认截图通道与分辨率可用
        started = time.monotonic()  # 时长只统计实际监听窗口，不含 MAA 连接开销
        proc = subprocess.Popen([adb, "-s", serial, "shell", "getevent", "-lt", device["path"]],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                encoding="utf-8", errors="replace", bufsize=1)
        listener = threading.Thread(target=listen, name="teach-getevent", daemon=True)
        listener.start()
        deadline = time.monotonic() + float(duration_s)  # 时长只算监听窗口
        while time.monotonic() < deadline and not stop_listening.is_set():
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        stop_listening.set()
        if proc.poll() is None:
            proc.terminate()
        listener.join(timeout=10)
        if listener.is_alive():
            raise RuntimeError("getevent 监听线程未在 10s 内退出")
        if errors:
            raise RuntimeError("; ".join(errors))
        detector.finish()
        if not detector.gestures:
            tail = " / ".join(raw_lines[-3:]) or f"returncode={proc.returncode}"
            raise RuntimeError(f"没有捕获到任何触摸手势：{tail}")
    finally:
        stop_listening.set()
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            subprocess.run([adb, "-s", serial, "shell", "pkill", "-INT", "getevent"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        if asst is not None:
            try:
                if not asst.stop():
                    raise RuntimeError("MAA 示教实例停止失败")
            finally:
                del asst

    elapsed = round(time.monotonic() - started, 3)
    raw_path = folder / "touch-raw.txt"
    raw_path.write_text("\n".join(raw_lines) + ("\n" if raw_lines else ""), encoding="utf-8")

    actions = build_actions(detector.gestures, meta=meta, resolution=resolution)
    step_frames = []
    for capture in captures:
        gesture = next((g for g in detector.gestures if g["index"] == capture["index"]), None)
        if gesture is None:
            continue
        if capture["kind"] == "down":
            step_frames.append({
                "index": gesture["index"] + 1,
                "label": f"{gesture['kind']} down",
                "frame": capture["frame"],
                "point": gesture["screen"]["down"],
                "arrow": ((gesture["screen"]["down"][0], gesture["screen"]["down"][1],
                           gesture["screen"]["up"][0], gesture["screen"]["up"][1])
                          if gesture["kind"] == "drag" else None),
            })
        else:
            step_frames.append({
                "index": gesture["index"] + 1,
                "label": f"{gesture['kind']} up",
                "frame": capture["frame"],
                "point": gesture["screen"]["up"],
                "arrow": None,
            })

    strip_path = None
    if step_frames:
        strip_path = compose_teach_strip(step_frames, out_path=folder / "demo-strip.png",
                                         stage_code=code)

    result = {
        "ok": True,
        "stage_code": code,
        "duration_s": elapsed,
        "device": {key: device[key] for key in ("path", "name", "x_code", "y_code",
                                                "x_range", "y_range", "multitouch")},
        "resolution": list(resolution),
        "actions": actions,
        "gesture_count": len(detector.gestures),
        "frame_count": len(captures),
        "raw_log": str(raw_path.resolve()),
        "json_path": str((folder / "demo.json").resolve()),
        "strip_path": None if strip_path is None else str(strip_path.resolve()),
        "projection": None if meta is None else {
            "available": True, "width": meta["width"], "height": meta["height"],
            "source": meta["source"],
        },
        "note": "只读监听 getevent 与截图，未注入触摸；瓦片由像素反投影推算，部署前需人工复核",
    }
    (folder / "demo.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


def append_learned_ops(demo: dict, *, path=LEARNED_OPS) -> Path:
    """把一次示教动作流追加进知识库（只写骨架与示例格式，不触碰设备）。"""
    if not isinstance(demo, dict) or not demo.get("stage_code"):
        raise ValueError("示教记录缺少 stage_code")
    actions = demo.get("actions")
    if not isinstance(actions, list) or not actions:
        raise ValueError("示教记录没有可写入的动作流")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    recorded_at = demo.get("recorded_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not target.exists():
        target.write_text(_LEARNED_OPS_SKELETON, encoding="utf-8")

    lines = [
        "",
        f"### {demo['stage_code']} · {recorded_at}",
        f"- 时长 {demo.get('duration_s')}s；分辨率 {demo.get('resolution')}；"
        f"设备 {(demo.get('device') or {}).get('path')} "
        f"({(demo.get('device') or {}).get('name')})",
        "- 瓦片由像素反投影推算，未经人工复核；落不到地块的动作为 `null`",
        "",
        "| # | t (s) | type | screen | end_screen | tile | direction |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for index, action in enumerate(actions, start=1):
        lines.append(
            f"| {index} | {action.get('t')} | {action.get('type')} | {action.get('screen')} | "
            f"{action.get('end_screen')} | {action.get('tile') or action.get('tile_end')} | "
            f"{action.get('direction')} |"
        )
    lines.extend([
        "",
        "<details><summary>原始动作流</summary>",
        "",
        "```json",
        json.dumps({"stage": demo["stage_code"], "actions": actions},
                   ensure_ascii=False, indent=2),
        "```",
        "",
        "</details>",
        "",
    ])
    with target.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    return target


_LEARNED_OPS_SKELETON = """# 人类示教沉淀：部署手感账本

由 `src/engine/teach_recorder.py` 只读录制生成：只监听 `getevent` 与截图，
不注入触摸。瓦片列是反投影推算值，**部署前必须人工复核**。

## 生成方式

```python
from src.engine.teach_recorder import record_human_demo, append_learned_ops

demo = record_human_demo("1-7", 60)  # 只读监听 60s，不注入触摸
append_learned_ops(demo)             # 追加到本文件
```

## 字段

| 字段 | 含义 |
| --- | --- |
| `t` | 相对录制开始的秒数 |
| `type` | `TAP`（点按）或 `DRAG`（拖拽） |
| `screen` / `end_screen` | 按下点 / 抬起点，1280×720 基准 |
| `tile` / `tile_end` | 反投影瓦片坐标；UI 区域或缺少 Tile-Pos 时为 `null` |
| `direction` | 拖拽主轴 `up`/`down`/`left`/`right`，点按为 `null` |

## 示例条目（格式参考，非真实录制）

```json
{
  "stage": "1-7",
  "actions": [
    {"t": 0.4, "type": "TAP", "screen": [640, 360], "end_screen": [640, 360],
     "tile": [4, 2], "tile_end": [4, 2], "direction": null},
    {"t": 3.1, "type": "DRAG", "screen": [120, 640], "end_screen": [700, 300],
     "tile": null, "tile_end": [5, 3], "direction": "right"}
  ]
}
```

## 已录制条目
"""
