#!/usr/bin/env python3
"""离线解析 MAA 回调并绘制失败证据；不执行截图、录屏或 OCR。"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageOps, ImageStat

ROOT = Path(__file__).resolve().parents[2]

HP_ROI = (560, 6, 200, 64)  # 1280×720 基准，唯一生命点 ROI 定义。
_HP_MIN_PIXELS = 4  # 单颗生命菱形约 10–15 px；下限必须小于半颗，否则漏检


def hp_signature(frame) -> tuple[int, int]:
    """返回 (亮青菱形像素数, ROI 亮度均值)，不使用 OCR。

    B>140 且 B>R+40；ROI 归一化到基准尺寸，避免分辨率改变阈值。
    """
    x, y, width, height = HP_ROI
    sx, sy = frame.width / 1280, frame.height / 720
    region = frame.crop((round(x * sx), round(y * sy),
                         round((x + width) * sx), round((y + height) * sy)))
    if region.size != (width, height):
        region = region.resize((width, height), Image.Resampling.NEAREST)
    region = region.convert("RGB")
    raw = region.tobytes()  # RGB 逐像素 3 字节，顺序与 getdata() 的 (r, g, b) 一致
    cyan = sum(raw[i + 2] > 140 and raw[i + 2] > raw[i] + 40
               for i in range(0, len(raw), 3))
    return cyan, round(ImageStat.Stat(region.convert("L")).mean[0])


HP_DIGIT_ROI = (724, 8, 64, 52)  # 1280×720 基准：剩余生命数字（粉红色）


def hp_digit_signature(frame) -> int:
    """剩余生命数字区域的粉色像素数；数字变化会导致计数跳变。"""
    x, y, w, h = HP_DIGIT_ROI
    sx, sy = frame.width / 1280, frame.height / 720
    region = frame.crop((round(x * sx), round(y * sy),
                         round((x + w) * sx), round((y + h) * sy))).convert("RGB")
    px = region.load()
    count = 0
    for j in range(region.height):
        for i in range(region.width):
            r, g, b = px[i, j]
            if r > 170 and r > g + 40 and r > b + 20:
                count += 1
    return count


def hp_digit_changed(prev: int, cur: int) -> bool:
    """数字位发生实质变化（不是抗锯齿抖动）。"""
    return abs(prev - cur) >= 8


def hp_digit_roi(frame):
    """裁出剩余生命数字区域（灰度）。"""
    x, y, w, h = HP_DIGIT_ROI
    sx, sy = frame.width / 1280, frame.height / 720
    return frame.crop((round(x * sx), round(y * sy),
                       round((x + w) * sx), round((y + h) * sy))).convert("L")


def hp_digit_diff(base_frame, frame) -> float:
    """与基线数字区域的平均灰度差；数字改变会持续高于阈值。

    实测 3→2 的差值约 13，静止 HUD 的同帧抖动远低于 8，因此比像素计数稳定。
    """
    a = hp_digit_roi(base_frame)
    b = hp_digit_roi(frame)
    ba, bb = a.tobytes(), b.tobytes()
    if len(ba) != len(bb):
        return 255.0
    return sum(abs(p - q) for p, q in zip(ba, bb)) / len(ba)


DIGIT_TIGHT_ROI = (748, 16, 28, 28)  # 1280×720：生命数字字形的紧致窗口
_HP_DIGIT_MIN_HITS = 24  # 基准窗口上「一位数字已完整画出」的下限，渐入帧只有个位数
# 基线收敛判据：相邻两帧命中数停止爬升且亮度不再升高。
_HP_DIGIT_STABLE_HITS = 3      # 命中数允许的绝对抖动
_HP_DIGIT_STABLE_RATIO = 0.05  # 命中数允许的相对抖动
_HP_DIGIT_STABLE_BRIGHTNESS = 0.25  # 紧致 ROI 灰度均值单帧可接受的上升量
_HP_DIGIT_STABLE_GRAY = 3.0    # 相邻帧数字 ROI 平均灰度差上限
# 命中数显著增加（渐入变亮）不是字形变化；真实生命变化只会让命中数下降。
_HP_DIGIT_FADE_GROWTH = 0.10
_HP_DIGIT_FADE_GROWTH_MIN = 4


def hp_digit_shape(frame) -> tuple[bytes, int]:
    """生命数字的紧致粉色掩膜与命中数，统一重采样到 1280×720 基准窗口。

    实机截图是 MAA 下采样的 1280×720，而录制素材/测试帧常是 640×360；旧实现
    按原始像素计数比较，同一画面两种分辨率差 4 倍，绝对阈值（base_n<20）会让
    低分辨率或 HUD 渐入帧上的检测永久失效。这里先按基准坐标裁切再最近邻重采样
    成 28×28，位宽与命中数都与分辨率无关。

    整屏被部署面板压暗时，粉色像素会塌缩到基线的一半以下；因此调用方必须先
    看命中数比例再比形状，否则「选人/部署压暗」会被误判成掉血。
    """
    x, y, w, h = DIGIT_TIGHT_ROI
    sx, sy = frame.width / 1280, frame.height / 720
    part = frame.crop((round(x * sx), round(y * sy),
                       round((x + w) * sx), round((y + h) * sy)))
    if part.size != (w, h):
        part = part.resize((w, h), Image.Resampling.NEAREST)
    raw = part.convert("RGB").tobytes()
    bits = bytearray(w * h)
    for i in range(len(bits)):
        r, g, b = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
        bits[i] = 1 if (r > 150 and g < 150 and r > b + 25) else 0
    return bytes(bits), sum(bits)


def hp_digit_ready(shape: tuple[bytes, int]) -> bool:
    """数字字形是否已完整绘出。

    HUD 渐入的那一两帧生命点菱形已经出现（``is_battle_hud`` 成立），数字却只有
    个位数命中；实测把它当基线后，``hp_digit_shape_changed`` 的基线保护会让之后
    所有比较都返回 False，整局静默漏报。基线只能锁定在 ready 的连续两帧上。
    """
    return shape[1] >= _HP_DIGIT_MIN_HITS


def hp_digit_brightness(frame) -> float:
    """紧致数字窗口的灰度均值；渐入时逐帧升高，画完才停止。"""
    x, y, w, h = DIGIT_TIGHT_ROI
    sx, sy = frame.width / 1280, frame.height / 720
    part = frame.crop((round(x * sx), round(y * sy),
                       round((x + w) * sx), round((y + h) * sy)))
    return ImageStat.Stat(part.convert("L")).mean[0]


def hp_digit_converged(prev: tuple[bytes, int], cur: tuple[bytes, int], *,
                       prev_frame=None, cur_frame=None) -> bool:
    """相邻两帧的数字掩膜是否已经收敛，可以安全锁定基线。

    HUD 渐入时，数字命中数和亮度可能持续上升。仅达到 ready 阈值且
    相邻字形差较小，并不代表最终字形稳定；过早锁定基线会将后续
    正常变亮误判为掉血。

    因此基线收敛必须同时满足：
      - 两帧都已 ready；
      - 命中数不再增加（``cur_n > prev_n`` 就是还在长）；
      - 命中数变化在抖动容差内（明显收缩可能是压暗/真实掉血，也不能锁）；
      - 给了帧时，紧致窗口亮度不再上升、且数字 ROI 平均灰度差足够小。
    """
    prev_n = prev[1]
    cur_n = cur[1]
    if not hp_digit_ready(prev) or not hp_digit_ready(cur):
        return False
    if cur_n > prev_n:  # 命中数仍在爬升：HUD 正在渐入
        return False
    tolerance = max(_HP_DIGIT_STABLE_HITS, round(_HP_DIGIT_STABLE_RATIO * prev_n))
    if prev_n - cur_n > tolerance:
        return False
    if prev_frame is not None and cur_frame is not None:
        if hp_digit_brightness(cur_frame) > (hp_digit_brightness(prev_frame)
                                             + _HP_DIGIT_STABLE_BRIGHTNESS):
            return False
        if hp_digit_diff(prev_frame, cur_frame) > _HP_DIGIT_STABLE_GRAY:
            return False
    return True


def hp_digit_shape_changed(base: tuple[bytes, int], cur: tuple[bytes, int]) -> bool:
    """字形结构是否改变；整屏压暗/遮挡/逐渐变亮按「不算掉血」处理。

    实测 diff/union：真实扣血 3→2 是 0.61、2→1 是 0.86（视频 38s/42s、70s/72s），
    同一数字 10s 内抖动只有 0.08；雪地图/选中干员面板是 0.05/0.14。压暗时粉色
    命中数会先塌缩到基线一半以下，这一步直接判「不算掉血」，所以阈值不能放宽。

    HUD 渐入时数字从暗到亮，绝对阈值下的掩膜会一路「长」出新像素：实测同一
    「3」字形亮度归一化后 IoU=0.915，但原始掩膜命中数能涨 40→84。这种情况下
    命中数显著增加只说明画面变亮，不是字形变化，必须直接返回 False；真实
    3→2/2→1 的命中数只会下降，不受影响。
    """
    base_bits, base_n = base
    cur_bits, cur_n = cur
    if base_n < _HP_DIGIT_MIN_HITS or cur_n < base_n * 0.5:
        return False
    growth = max(_HP_DIGIT_FADE_GROWTH_MIN, round(base_n * _HP_DIGIT_FADE_GROWTH))
    if cur_n > base_n + growth:
        return False
    diff = sum(1 for a, b in zip(base_bits, cur_bits) if a != b)
    overlap = sum(1 for a, b in zip(base_bits, cur_bits) if a and b)
    union = base_n + cur_n - overlap
    return union > 0 and diff / union > 0.4


def is_battle_hud(frame) -> bool:
    """顶部生命点区至少存在 ROI 面积 0.2% 的青蓝成分。

    只作生命传感的基线条件，不能用来判断战斗是否结束：暂停遮罩、选中干员
    压暗、PRTS/过场等 HUD 状态下生命点会消失，但战斗仍在进行。战斗画面
    分类一律走 battle_scene_state。
    """
    return hp_signature(frame)[0] >= _HP_MIN_PIXELS


BATTLE_CONTROL_ROI = (1165, 20, 75, 65)  # 1280×720 基准：右上暂停/播放控制块
BATTLE_PAUSE_BUTTON = "Battle/BattleFlag/BattleOfficiallyBegin.png"
BATTLE_PAUSE_MENU = "BattlePauseMenu.png"      # 暂停态：右上播放三角
_PAUSE_BUTTON_MIN_SCORE = 0.80
_PAUSE_MENU_MIN_SCORE = 0.85


def _battle_control_score(frame, template_path) -> float:
    """在战斗控制块 ROI 内做灰度归一化模板匹配，返回最高相关分。"""
    import cv2

    x, y, width, height = BATTLE_CONTROL_ROI
    sx, sy = frame.width / 1280, frame.height / 720
    crop = frame.crop((round(x * sx), round(y * sy),
                       round((x + width) * sx), round((y + height) * sy)))
    if crop.size != (width, height):
        crop = crop.resize((width, height), Image.Resampling.NEAREST)
    image = np.asarray(crop.convert("L"), dtype=np.uint8)
    template = np.asarray(Image.open(template_path).convert("L"), dtype=np.uint8)
    if template.shape[0] > image.shape[0] or template.shape[1] > image.shape[1]:
        return 0.0
    result = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
    score = float(np.max(result))
    return score if math.isfinite(score) else 0.0


def battle_scene_state(frame, *, live_template: Path, paused_template: Path) -> str:
    """战斗画面分类：live / paused / not_battle / unknown。

    右上控制块用本机 MAA 的暂停双竖条模板和本地配置的播放三角模板分类；
    暂停、选中干员遮罩与压暗时不能用生命点消失判断退出。

    返回值语义：
      - ``live``/``paused``：战斗仍在进行，绝不能当退出；
      - ``not_battle``：看不到战斗控制块，但**不等于已退出**（加载、结算、
        未知页面同样没有控制块），退出必须另有原生证据；
      - ``unknown``：模板缺失、抓帧或匹配异常，任何情况下都不得当退出。
    """
    if frame is None:
        return "unknown"
    try:
        if _battle_control_score(frame, live_template) >= _PAUSE_BUTTON_MIN_SCORE:
            return "live"
        if _battle_control_score(frame, paused_template) >= _PAUSE_MENU_MIN_SCORE:
            return "paused"
    except Exception:
        return "unknown"
    return "not_battle"


RESULT_BANNER_BOX = (0, 110, 560, 440)  # 1280×720：MAA 结算 OCR ROI 内的大字标题带
_RESULT_GLYPH_WHITE = 245  # 纯白字；地图雪地/高光/UI 蓝框都达不到三通道 245
_RESULT_GLYPH_MIN_W, _RESULT_GLYPH_MAX_W = 12, 90
_RESULT_GLYPH_MIN_H, _RESULT_GLYPH_MAX_H = 32, 90
_RESULT_GLYPH_MIN_AREA = 250
_RESULT_BASELINE_TOLERANCE = 10
_RESULT_MIN_GLYPHS = 4
_RESULT_MIN_SPAN, _RESULT_MAX_SPAN = 180, 360


def _settlement_like(frame) -> bool:
    """原生结算标题「任务失败/行动结束」：左侧同一基线上一排 ≥4 个纯白大字。

    旧实现用中央横带近白像素占比，雪地图大片雪面会误判（真实雪图帧 0.02–0.06，
    与结算帧重叠）。这里改为几何/文字带特征：只有结算页左侧结果横幅才有
    50–70px 高、四字对齐的纯白标题；地图高光、蓝色地块框、干员立绘和技能
    文字都不满足「同基线四字」。检测失败时返回 False（保留画面给 AI 复核）。
    """
    try:
        import cv2
        if frame.size != (1280, 720):
            frame = frame.resize((1280, 720))
        rgb = np.asarray(frame.convert("RGB"), dtype=np.int16)
    except Exception:
        return False
    white = ((rgb[:, :, 0] >= _RESULT_GLYPH_WHITE)
             & (rgb[:, :, 1] >= _RESULT_GLYPH_WHITE)
             & (rgb[:, :, 2] >= _RESULT_GLYPH_WHITE))
    left, top, right, bottom = RESULT_BANNER_BOX
    mask = white[top:bottom, left:right].astype(np.uint8)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
    rows: list[list[tuple[int, int, int, int]]] = []
    for x, y, w, h, area in stats[1:count]:
        if not (_RESULT_GLYPH_MIN_W <= w <= _RESULT_GLYPH_MAX_W
                and _RESULT_GLYPH_MIN_H <= h <= _RESULT_GLYPH_MAX_H
                and area >= _RESULT_GLYPH_MIN_AREA):
            continue
        glyph = (int(x), int(y) + top, int(w), int(h))
        glyph_bottom = glyph[1] + glyph[3]
        for row in rows:
            first = row[0]
            if abs((first[1] + first[3]) - glyph_bottom) <= _RESULT_BASELINE_TOLERANCE:
                row.append(glyph)
                break
        else:
            rows.append([glyph])
    for row in rows:
        if len(row) < _RESULT_MIN_GLYPHS:
            continue
        span = max(g[0] + g[2] for g in row) - min(g[0] for g in row)
        if _RESULT_MIN_SPAN <= span <= _RESULT_MAX_SPAN:
            return True
    return False


# 1280×720 基准：作战结算页左侧奖章带（x<30%、y∈[35%,58%]）。
RESULT_STAR_ROI = (0, 252, 384, 166)
_STAR_MIN_BLUE = 100        # 蓝色奖章亮度下限；灰色奖章 B≈R≈G 直接落选
_STAR_BLUE_OVER_RED = 1.3   # 蓝必须显著压过红，排除白色标题/雪地高光
_STAR_BLUE_OVER_GREEN = 1.15  # 排除青色 HUD 点
_STAR_CLOSE_KERNEL = 9      # 奖章内部镂空/描边后按整体计数
_STAR_MIN_AREA = 80
_STAR_MIN_SIDE, _STAR_MAX_SIDE = 20, 120
_STAR_MIN_COUNT = 3         # 三星通关的证据就是三枚蓝色奖章


def result_three_star(frame) -> bool:
    """结算页是否确有三枚蓝色通关奖章——胜利的唯一视觉证据。

    「行动结束/MISSION RESULTS」这类通用标题只能证明战斗结束，1/2 星与
    任务失败页也都用同一版式。真正的三星结算在左侧奖章带留下三枚亮蓝
    奖章；失败/中断页是灰色奖章，蓝色掩膜一枚都取不到。

    归一化 1280×720 后在 ``RESULT_STAR_ROI`` 内取 B>100 且 B>1.3R 且
    B>1.15G 的像素，9×9 闭运算后只数面积≥80、宽高 20–120 的连通域，
    ≥3 枚才算三星。无法解码、尺寸异常或 cv2 不可用一律返回 False：
    未知不判胜，调用方只能降级为「战斗结束」。
    """
    if frame is None:
        return False
    try:
        width, height = frame.size
    except Exception:
        return False
    # 小于 64×36 的画面连一枚 20px 奖章都放不下，按尺寸异常处理。
    if width < 64 or height < 36:
        return False
    try:
        import cv2
        if frame.size != (1280, 720):
            frame = frame.resize((1280, 720))
        rgb = np.asarray(frame.convert("RGB"), dtype=np.int16)
    except Exception:
        return False
    red, green, blue = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = ((blue > _STAR_MIN_BLUE)
            & (blue > red * _STAR_BLUE_OVER_RED)
            & (blue > green * _STAR_BLUE_OVER_GREEN)).astype(np.uint8)
    left, top, roi_w, roi_h = RESULT_STAR_ROI
    roi = mask[top:top + roi_h, left:left + roi_w]
    try:
        kernel = cv2.getStructuringElement(
            cv2.MORPH_RECT, (_STAR_CLOSE_KERNEL, _STAR_CLOSE_KERNEL))
        closed = cv2.morphologyEx(roi, cv2.MORPH_CLOSE, kernel)
        count, _, stats, _ = cv2.connectedComponentsWithStats(closed, 8)
    except Exception:
        return False
    stars = 0
    for _x, _y, comp_w, comp_h, area in stats[1:count]:
        if area < _STAR_MIN_AREA:
            continue
        if not (_STAR_MIN_SIDE <= comp_w <= _STAR_MAX_SIDE
                and _STAR_MIN_SIDE <= comp_h <= _STAR_MAX_SIDE):
            continue
        stars += 1
    return stars >= _STAR_MIN_COUNT


def select_evidence_frames(frames: list, *, need: int = 4, include_last: bool = False) -> list:
    """默认跳过末帧；漏怪冻结时保留案发末帧，从合格帧中选最近 need 张。

    过滤规则：冻结后帧（info 带 sampled_after_freeze=True）、纯黑/加载帧
    （灰度均值<18 或标准差<3）、原生结算标题帧（左侧同基线四字纯白大字）。
    候选不足时复制最后一张合格帧；完全没有合格帧时返回空列表，不伪造截图。
    """
    if need <= 0:
        raise ValueError("证据帧数必须为正数")
    selected = []
    for frame in reversed(frames if include_last else frames[:-1]):
        if frame.info.get("sampled_after_freeze") is True:
            continue
        gray = frame.convert("L")
        from PIL import ImageStat
        stats = ImageStat.Stat(gray)
        if stats.mean[0] < 18 or stats.stddev[0] < 3:
            continue
        if _settlement_like(frame):
            continue
        selected.append(frame)
        if len(selected) >= need:
            break
    selected.reverse()
    if selected and len(selected) < need:
        selected.extend([selected[-1]] * (need - len(selected)))
    return selected


def load_projection_meta(stage_code: str, *, maa_dir: str | None = None) -> dict | None:
    """读取 MAA Tile-Pos，返回 {width,height,view,height_types} 供网格投影。"""
    import glob as _glob
    import os as _os
    from pathlib import Path as _Path

    root = _Path(_os.environ.get("MAA_DIR", "maa")) if maa_dir is None else _Path(maa_dir)
    intel = ROOT / "data" / "levels" / f"{stage_code}-intel.json"
    stage_id = None
    if intel.exists():
        stage_id = json.loads(intel.read_text(encoding="utf-8")).get("stage", {}).get("stageId")
    patterns = [f"{stage_id}-activities-*"] if stage_id else []
    patterns.append(f"*{stage_code}*")
    for pat in patterns:
        files = _glob.glob(str(root / "resource" / "Arknights-Tile-Pos" / pat))
        if not files:
            continue
        data = json.loads(_Path(files[0]).read_text(encoding="utf-8"))
        if "view" not in data:
            continue
        return {
            "width": int(data["width"]),
            "height": int(data["height"]),
            "view": data["view"],
            "height_types": [[int(t.get("heightType", 0)) for t in row] for row in data["tiles"]],
            "source": files[0],
        }
    return None


def compose_evidence_grid(
    frames: list, *, stage_code: str, out_path: Path, tile_grid: dict | None = None,
) -> Path:
    """按先后顺序拼成 2×2 PNG；有 Tile-Pos 视口信息时按等距投影画真实地块网格。

    `tile_grid` 支持两种形态：
      - 含 width/height/view/height_types → 用投影矩阵画贴合地块的多边形网格；
      - 仅含 width/height（旧 intel tiles）→ 退回均匀逻辑网格，并在图上标注 logical。
    """
    if len(frames) > 4:
        raise ValueError("证据拼图最多接受 4 帧")
    width, height = frames[0].size if frames else (640, 360)
    canvas = Image.new("RGB", (width * 2, height * 2), "#161b22")
    draw = ImageDraw.Draw(canvas)
    for index in range(4):
        left, top = (index % 2) * width, (index // 2) * height
        if index < len(frames):
            frame = ImageOps.contain(frames[index].convert("RGB"), (width, height))
            canvas.paste(frame, (left + (width - frame.width) // 2,
                                 top + (height - frame.height) // 2))
        else:
            draw.text((left + width // 2 - 30, top + height // 2),
                      "(no frame)", fill="white")
        draw.rectangle((left, top, left + 170, top + 23), fill="#161b22")
        draw.text((left + 6, top + 5), f"#{index + 1} {stage_code}", fill="white")
        draw.rectangle((left, top, left + width - 1, top + height - 1), outline="#6e7681")

    if tile_grid is not None:
        cols, rows = int(tile_grid["width"]), int(tile_grid["height"])
        if cols <= 0 or rows <= 0:
            raise ValueError("Tile-Pos 网格宽高必须为正数")
        view = tile_grid.get("view")
        height_types = tile_grid.get("height_types")
        if view and height_types:
            from src.engine.tile_projection import tile_corners, tile_screen_pos

            for index in range(4):
                left = (index % 2) * width
                top = (index // 2) * height
                for ty in range(rows):
                    for tx in range(cols):
                        ht = height_types[ty][tx] if ty < len(height_types) and tx < len(height_types[ty]) else 0
                        pts = tile_corners(tx, ty, width=cols, height=rows, height_type=ht,
                                           view=view, screen=(width, height))
                        poly = [(left + px, top + py) for px, py in pts]
                        draw.polygon(poly, outline="#ffffff")
                        if (tx + ty) % 2 == 0:
                            cx, cy = tile_screen_pos(tx, ty, width=cols, height=rows,
                                                     height_type=ht, view=view,
                                                     screen=(width, height))
                            if 0 <= cx < width and 0 <= cy < height:
                                draw.text((left + cx - 8, top + cy - 5), f"{tx},{ty}", fill="#ffe082")
            draw.text((180, 5), "Tile-Pos projected grid (x,y at tile center)", fill="#ffe082")
        else:
            x0, y0, x1, y1 = 28, 48, width - 12, height - 16
            if x1 <= x0 or y1 <= y0:
                raise ValueError("证据帧过小，无法标注 Tile-Pos 网格")
            dx, dy = (x1 - x0) / cols, (y1 - y0) / rows
            for x in range(cols + 1):
                px = round(x0 + x * dx)
                draw.line((px, y0, px, y1), fill="#ffe082")
            for y in range(rows + 1):
                py = round(y0 + y * dy)
                draw.line((x0, py, x1, py), fill="#ffe082")
            draw.text((180, 5), "logical grid (no viewport data)", fill="#ffe082")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG")
    return out_path


def parse_maa_events(events_path: Path) -> dict:
    """解析逐行原生回调；失败优先于 AllTasksCompleted，动作序号从 0 开始。

    事件号为 -3 的是 runner 标记的「预期未命中」（结果探测链没识别到关键字），
    只做诊断记录，不参与胜负/状态判定。

    ``what == "RunnerWarning"`` 是 runner 的收尾清理失败（录屏拉取、复核清单/
    sidecar 写入），同样只丢证据：不论它落在哪个消息号，都不能把已复核的胜利
    映射成 ``failed``。真正的运行失败仍由 ``RunnerError`` / ``CaptureError`` /
    失败消息号表达。
    """
    action_count = 0
    last_action_index = None
    last_action = None
    errors: list[str] = []
    program_status = "unknown"
    failure_messages = {0, 1, 10000, 10004, 20000}
    with Path(events_path).open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
                raw_details = event["details"]
                details = json.loads(raw_details) if isinstance(raw_details, str) else raw_details
                if not isinstance(details, dict):
                    raise ValueError("details 必须是 JSON 对象")
                message = int(event["message"])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"{events_path}:{line_number}: MAA 事件格式错误：{exc}") from exc
            nested = details.get("details") or {}
            what = details.get("what", "")
            why = details.get("why") or nested.get("why")
            if what == "RunnerWarning":
                # 收尾清理警告不属于运行结果：无论消息号如何都不参与状态判定。
                continue
            if message == -3:
                continue
            if what == "CopilotAction":
                action_count += 1
                last_action_index = nested.get("action_index", nested.get("index", action_count - 1))
                action = nested.get("action", "")
                target = nested.get("target") or nested.get("name") or ""
                last_action = " ".join(str(part) for part in (action, target) if part) or None
                location = nested.get("location")
                if location is not None and last_action:
                    if isinstance(location, dict):
                        location = [location["x"], location["y"]]
                    last_action += f" @ ({location[0]},{location[1]})"
            if message in failure_messages or message == -1:
                first = details.get("first") or []
                label = (what or (first[0] if first else None)
                         or details.get("subtask")
                         or details.get("taskchain") or "MAA")
                detail = why if why and why != "Normal" else ""
                errors.append(f"message={message} {label}" + (f": {detail}" if detail else ""))
            if what == "RunnerTimeout":
                program_status = "timeout"
            elif message == 10000 or what in {"RunnerError", "CaptureError"}:
                if program_status != "timeout":
                    program_status = "failed"
            elif message == 10002 and program_status == "unknown":
                program_status = "completed"
    return {
        "action_count": action_count,
        "last_action_index": last_action_index,
        "last_action": last_action,
        "errors": errors,
        "program_status": program_status,
    }


def build_failure_report(
    *, events_path: Path, outdir: Path, frames: list, stage_code: str,
    tile_grid: dict | None = None, force: bool = False, reason: str | None = None,
) -> dict:
    """失败时选取冻结前最近四张合格帧；无合格截图时标记 no_valid_frames。"""
    parsed = parse_maa_events(events_path)
    status = parsed["program_status"]
    evidence_png = None
    if status in {"failed", "timeout"} or force:
        frames = select_evidence_frames(frames, include_last=reason == "leak")
    if (status in {"failed", "timeout"} or force) and frames:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        evidence_png = str(compose_evidence_grid(
            frames, stage_code=stage_code,
            out_path=Path(outdir) / f"evidence-{timestamp}.png", tile_grid=tile_grid,
        ))
    message = "; ".join(parsed["errors"]) or status
    if status == "timeout":
        message = f"timeout: {message}"
    reason = f"{reason}: {message}" if reason else message
    if (status in {"failed", "timeout"} or force) and not frames:
        reason = f"{reason}; no_valid_frames"
    return {
        "reason": reason,
        "last_action_index": parsed["last_action_index"],
        "last_action": parsed["last_action"],
        "evidence_png": evidence_png,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="离线解析 MAA 事件并生成战报与失败证据")
    ap.add_argument("--stage", default="unknown", help="战报中的关卡代号")
    ap.add_argument("--outdir", required=True, help="战报输出目录")
    ap.add_argument("--events", help="已有 maa-events.jsonl；默认取输出目录内同名文件")
    ap.add_argument("--tile-grid", help="网格 JSON 或含 tiles 字段的 intel JSON")
    args = ap.parse_args(argv)
    outdir = Path(args.outdir).resolve()
    events_path = Path(args.events).resolve() if args.events else outdir / "maa-events.jsonl"
    tile_grid = None
    if args.tile_grid:
        raw = json.loads(Path(args.tile_grid).read_text(encoding="utf-8"))
        tile_grid = raw.get("tiles", raw)
    parsed = parse_maa_events(events_path)
    # 离线保留最近 24 张原始帧，由同一选择器过滤并跳过最后的过渡帧。
    frame_paths = sorted(path for path in (outdir / "frames").glob("*")
                         if path.suffix.lower() in {".png", ".jpg", ".jpeg"})[-24:]
    frames = []
    for path in frame_paths:
        with Image.open(path) as image:
            frames.append(image.convert("RGB"))
    report = build_failure_report(events_path=events_path, outdir=outdir, frames=frames,
                                  stage_code=args.stage, tile_grid=tile_grid)
    summary = {"stage": args.stage, "events": str(events_path), **parsed, "report": report}
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 1 if parsed["program_status"] in {"failed", "timeout"} else 0


if __name__ == "__main__":
    sys.exit(main())
