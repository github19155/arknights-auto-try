"""Original geometric controls for offline battle-recognition tests."""
from pathlib import Path

from PIL import Image, ImageDraw


def battle_control(*, paused: bool) -> Image.Image:
    image = Image.new("RGB", (44 if paused else 47, 36), (27, 30, 34))
    draw = ImageDraw.Draw(image)
    light = (240, 239, 237)
    if paused:
        draw.polygon(((11, 5), (35, 18), (11, 31)), fill=light)
    else:
        draw.rectangle((9, 6, 16, 29), fill=light)
        draw.rectangle((30, 6, 37, 29), fill=light)
    return image


def write_battle_control_templates(live: Path, paused: Path) -> None:
    live.parent.mkdir(parents=True, exist_ok=True)
    paused.parent.mkdir(parents=True, exist_ok=True)
    battle_control(paused=False).save(live)
    battle_control(paused=True).save(paused)
