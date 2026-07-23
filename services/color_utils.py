"""颜色生成工具。

从 EMath3DVisualizer 提取的 HSV 距离感知随机颜色生成。
"""
import colorsys
import math
import random
from typing import Optional, Sequence


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    r, g, b = rgb
    return "#{:02x}{:02x}{:02x}".format(
        int(r * 255), int(g * 255), int(b * 255)
    )


def hex_to_rgb(hex_color: str) -> Optional[tuple[float, float, float]]:
    try:
        c_str = hex_color.lstrip('#')
        if len(c_str) == 6:
            return (
                int(c_str[0:2], 16) / 255.0,
                int(c_str[2:4], 16) / 255.0,
                int(c_str[4:6], 16) / 255.0,
            )
    except Exception:
        pass
    return None


def generate_random_color(existing_colors: Optional[Sequence[str]] = None) -> str:
    """生成随机颜色，并尽量避开已存在的颜色（RGB 空间距离检查）。

    使用 HSV 色彩空间，保证颜色饱和度与亮度适中，适合数据可视化。
    """
    existing = existing_colors or []
    existing_rgb = []
    for c in existing:
        rgb = hex_to_rgb(c)
        if rgb:
            existing_rgb.append(rgb)

    best_color = None
    max_min_dist = -1.0

    for _ in range(50):
        h = random.random()
        s = 0.6 + random.random() * 0.4  # 饱和度 0.6-1.0
        v = 0.7 + random.random() * 0.3  # 亮度 0.7-1.0
        r, g, b = colorsys.hsv_to_rgb(h, s, v)

        if not existing_rgb:
            return rgb_to_hex((r, g, b))

        min_dist = float('inf')
        for er, eg, eb in existing_rgb:
            dist = math.sqrt((r - er) ** 2 + (g - eg) ** 2 + (b - eb) ** 2)
            if dist < min_dist:
                min_dist = dist

        if min_dist > 0.25:  # 距离足够远，直接接受
            return rgb_to_hex((r, g, b))

        if min_dist > max_min_dist:
            max_min_dist = min_dist
            best_color = (r, g, b)

    if best_color:
        return rgb_to_hex(best_color)
    return rgb_to_hex((r, g, b))


# 预定义的专业配色方案（可用于默认分配或前端兜底）
PALETTE_3D = [
    "#2d7ef7",  # 蓝
    "#22c55e",  # 绿
    "#ef4444",  # 红
    "#a855f7",  # 紫
    "#f59e0b",  # 橙
    "#06b6d4",  # 青
    "#eab308",  # 黄
    "#ec4899",  # 粉
]
