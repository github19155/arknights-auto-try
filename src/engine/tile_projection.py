"""明日方舟战场瓦片坐标 → 屏幕像素投影（等距透视矩阵）。

只做纯数学：与 MAA `resource/Arknights-Tile-Pos/<stage>.json` 的
`width/height/tiles[].heightType/view` 对齐，输出 (x, y) 在 1280×720 基准
屏幕上的像素坐标，供证据图网格与可视化使用。

参考实现来自社区公开的 Arknights-Tile-Pos 投影矩阵常数（20°/30°/10°、
透视 near/far、9:16→3:4 的视口补偿），此处以纯 numpy 重写，不依赖 OpenCV。
`pixel_to_tile` 是它的解析逆：固定高度平面解齐次方程，不需要屏幕搜索。
"""
from __future__ import annotations

import math

import numpy as np


def _matrix_p(ratio: float) -> np.ndarray:
    return np.array([
        [ratio / math.tan(math.pi * 20 / 180), 0, 0, 0],
        [0, 1 / math.tan(math.pi * 20 / 180), 0, 0],
        [0, 0, -(1000 + 0.3) / (1000 - 0.3), -(1000 * 0.3 * 2) / (1000 - 0.3)],
        [0, 0, -1, 0],
    ], dtype=np.float64)


def _matrix_x() -> np.ndarray:
    return np.array([
        [1, 0, 0, 0],
        [0, math.cos(math.pi * 30 / 180), -math.sin(math.pi * 30 / 180), 0],
        [0, -math.sin(math.pi * 30 / 180), -math.cos(math.pi * 30 / 180), 0],
        [0, 0, 0, 1],
    ], dtype=np.float64)


def _matrix_y() -> np.ndarray:
    return np.array([
        [math.cos(math.pi * 10 / 180), 0, math.sin(math.pi * 10 / 180), 0],
        [0, 1, 0, 0],
        [-math.sin(math.pi * 10 / 180), 0, math.cos(math.pi * 10 / 180), 0],
        [0, 0, 0, 1],
    ], dtype=np.float64)


def viewport_adapter(ratio: float) -> tuple[float, float]:
    """9:16 到 3:4 的视口补偿（超出 16:9 的画面才生效）。"""
    from_ratio, to_ratio = 9 / 16, 3 / 4
    if ratio < from_ratio - 1e-5:
        return 0.0, 0.0
    t = (ratio - from_ratio) / (to_ratio - from_ratio)
    return -1.4 * t, -2.8 * t


def _world_to_screen(matrix: np.ndarray, pos: tuple[float, float, float],
                     screen_w: int, screen_h: int) -> tuple[float, float]:
    vec = np.array([pos[0], pos[1], pos[2], 1.0])
    x, y, _, w = matrix @ vec
    x = (1 + x / w) / 2
    y = (1 + y / w) / 2
    return x * screen_w, (1 - y) * screen_h


def tile_screen_pos(
    tile_x: float,
    tile_y: float,
    *,
    width: int,
    height: int,
    height_type: int = 0,
    view: list,
    screen: tuple[int, int] = (1280, 720),
    side: bool = True,
    focus: bool = False,
    focus_tile: tuple[float, float] | None = None,
) -> tuple[float, float]:
    """返回瓦片 (tile_x, tile_y) 的屏幕像素坐标。

    tile_x 从左到右 0..width-1；tile_y 自上而下 0..height-1（与 MAA 一致）。
    `side=True` 用战斗中的倾斜视角（部署时的观察面）。

    `focus=True` 保留旧语义：相机平移到本格。此时所有同高度格投影重合，
    无法反投影；要固定观察同一屏多格请给 `focus_tile=(x, y)`，它优先于
    `focus`，并且是 `pixel_to_tile` 严格互逆的前提。
    """
    camera_focus: tuple[float, float] | None = None
    if focus_tile is not None:
        camera_focus = (float(focus_tile[0]), float(focus_tile[1]))
    elif focus:
        camera_focus = (float(tile_x), float(tile_y))

    world = _tile_world(tile_x, tile_y, width, height, height_type)
    matrix = _camera_matrix(width=width, height=height, view=view, screen=screen,
                            side=side, focus_tile=camera_focus)
    return _world_to_screen(matrix, world, *screen)


def _tile_world(tile_x: float, tile_y: float, width: int, height: int,
                height_type: float) -> tuple[float, float, float]:
    """瓦片坐标 → 世界坐标（以地图中心为原点，y 轴朝上，高台每级抬 0.4）。"""
    return (float(tile_x) - (width - 1) / 2,
            (height - 1) / 2 - float(tile_y),
            float(height_type) * -0.4)


def _camera_matrix(*, width: int, height: int, view: list,
                   screen: tuple[int, int], side: bool = True,
                   focus_tile: tuple[float, float] | None = None) -> np.ndarray:
    """构建世界坐标 → 裁切空间矩阵；`focus_tile` 固定相机平移，投影才可逆。"""
    screen_w, screen_h = screen
    ratio = screen_h / screen_w
    offset_x = offset_y = 0.0
    if focus_tile is not None:
        offset_x = float(focus_tile[0]) - (width - 1) / 2
        offset_y = (height - 1) / 2 - float(focus_tile[1])

    adapter_y, adapter_z = viewport_adapter(ratio)
    base = view[1] if side else view[0]
    x = float(base[0]) + offset_x
    y = float(base[1]) + offset_y + adapter_y
    z = float(base[2]) + adapter_z

    raw = np.array([
        [1, 0, 0, -x],
        [0, 1, 0, -y],
        [0, 0, 1, -z],
        [0, 0, 0, 1],
    ], dtype=np.float64)
    matrix = _matrix_x() @ _matrix_y() @ raw if side else _matrix_x() @ raw
    return _matrix_p(ratio) @ matrix


def _solve_plane(matrix: np.ndarray, ndc_x: float, ndc_y: float,
                 cz: float) -> tuple[float, float] | None:
    """固定高度 cz 时反解世界 (cx, cy)；投影平面退化返回 None。"""
    a, b = ndc_x, ndc_y
    lhs = np.array([
        [matrix[0, 0] - a * matrix[3, 0], matrix[0, 1] - a * matrix[3, 1]],
        [matrix[1, 0] - b * matrix[3, 0], matrix[1, 1] - b * matrix[3, 1]],
    ], dtype=np.float64)
    rhs = -np.array([
        (matrix[0, 2] - a * matrix[3, 2]) * cz + (matrix[0, 3] - a * matrix[3, 3]),
        (matrix[1, 2] - b * matrix[3, 2]) * cz + (matrix[1, 3] - b * matrix[3, 3]),
    ], dtype=np.float64)
    if abs(float(np.linalg.det(lhs))) < 1e-12:
        return None
    cx, cy = np.linalg.solve(lhs, rhs)
    return float(cx), float(cy)


def _height_lookup(height_type):
    """归一化高度入参 → (候选高度列表, 每格高度查询函数或 None)。"""
    if isinstance(height_type, dict):
        table = {(int(key[0]), int(key[1])): int(value) for key, value in height_type.items()}
        return sorted(set(table.values())), lambda x, y: table.get((x, y))
    iterable = hasattr(height_type, "__iter__") and not isinstance(height_type, (str, bytes))
    if iterable:
        grid = []
        for row in height_type:
            cells = row if hasattr(row, "__iter__") and not isinstance(row, (str, bytes)) else [row]
            grid.append([int(value) for value in cells])

        def lookup(x: int, y: int) -> int | None:
            if 0 <= y < len(grid) and 0 <= x < len(grid[y]):
                return grid[y][x]
            return None

        return sorted({value for row in grid for value in row}), lookup
    if isinstance(height_type, bool) or not isinstance(height_type, int):
        raise ValueError("height_type 必须是整数、二维高度网格或 {(x, y): 高度} 字典")
    return [height_type], None


def pixel_to_tile(
    px: float,
    py: float,
    *,
    width: int,
    height: int,
    height_type,
    view: list,
    screen: tuple[int, int] = (1280, 720),
    side: bool = True,
    focus: bool = False,
    focus_tile: tuple[float, float] | None = None,
    tolerance: float = 1e-6,
) -> tuple[int, int] | None:
    """屏幕像素反投影回瓦片坐标；落在图外或全部候选都越界时返回 None。

    对每个候选高度（整数、`height_types[y][x]` 网格或 `{(x, y): 高度}` 字典）
    固定 `cz` 解齐次线性方程，只保留落在地图外边界（格心 ±0.5）以内且
    高度网格自洽的解；多解时取离格心最近的一个。因此以格心投影为输入时与
    `tile_screen_pos` 严格互逆（浮点误差 < 1e-9）。

    `focus=True` 而没给 `focus_tile` 时直接抛 ValueError：旧 focus 语义把
    相机平移到目标格，所有同高度格投影到同一点，反投影无从区分。
    """
    if isinstance(width, bool) or isinstance(height, bool) or width <= 0 or height <= 0:
        raise ValueError("地图宽高必须是正整数")
    screen_w, screen_h = screen
    if screen_w <= 0 or screen_h <= 0:
        raise ValueError("屏幕尺寸必须为正数")
    if focus and focus_tile is None:
        raise ValueError("focus=True 且未给 focus_tile：同高度格投影重合，无法反算瓦片")
    if focus_tile is not None:
        fx, fy = focus_tile
        if not (0 <= fx <= width - 1 and 0 <= fy <= height - 1):
            raise ValueError(f"焦点格越界：{focus_tile}，地图为 {width}×{height}")
        camera_focus: tuple[float, float] | None = (float(fx), float(fy))
    else:
        camera_focus = None

    heights, lookup = _height_lookup(height_type)
    matrix = _camera_matrix(width=width, height=height, view=view, screen=screen,
                            side=side, focus_tile=camera_focus)
    ndc_x = 2.0 * float(px) / screen_w - 1.0
    ndc_y = 1.0 - 2.0 * float(py) / screen_h

    best: tuple[float, int, int] | None = None
    for level in heights:
        solved = _solve_plane(matrix, ndc_x, ndc_y, float(level) * -0.4)
        if solved is None:
            continue
        cx, cy = solved
        tile_xf = cx + (width - 1) / 2
        tile_yf = (height - 1) / 2 - cy
        # 地图外边界是格心 ±0.5；再远就不属于任何地块，不能就近吸附。
        if not (-0.5 - tolerance <= tile_xf <= width - 0.5 + tolerance):
            continue
        if not (-0.5 - tolerance <= tile_yf <= height - 0.5 + tolerance):
            continue
        tile_x = min(max(int(round(tile_xf)), 0), width - 1)
        tile_y = min(max(int(round(tile_yf)), 0), height - 1)
        if lookup is not None and lookup(tile_x, tile_y) != level:
            continue
        distance = math.hypot(tile_xf - tile_x, tile_yf - tile_y)
        if best is None or distance < best[0]:
            best = (distance, tile_x, tile_y)
    return None if best is None else (best[1], best[2])


def infer_drag_direction(x1: float, y1: float, x2: float, y2: float) -> str:
    """按位移主轴判拖拽方向；两轴等幅时归到水平轴，零位移抛 ValueError。"""
    dx = float(x2) - float(x1)
    dy = float(y2) - float(y1)
    if dx == 0.0 and dy == 0.0:
        raise ValueError("拖拽起终点重合，方向未定")
    if abs(dx) >= abs(dy):
        return "right" if dx > 0 else "left"
    return "down" if dy > 0 else "up"


def tile_corners(
    tile_x: int,
    tile_y: int,
    *,
    width: int,
    height: int,
    height_type: int = 0,
    view: list,
    screen: tuple[int, int] = (1280, 720),
    side: bool = True,
) -> list[tuple[float, float]]:
    """返回该瓦片四角（左上、右上、右下、左下）的屏幕坐标。"""
    pts = []
    for dx, dy in ((-0.5, -0.5), (0.5, -0.5), (0.5, 0.5), (-0.5, 0.5)):
        pts.append(tile_screen_pos(tile_x + dx, tile_y + dy, width=width, height=height,
                                   height_type=height_type, view=view, screen=screen,
                                   side=side))
    return pts
