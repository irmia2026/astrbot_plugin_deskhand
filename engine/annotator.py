"""
annotator.py — 截图 + 控件标注（可选视觉反馈）。

- screenshot(): PIL ImageGrab 截屏（指定窗口或全屏）
- annotate(img, tree_state): 在截图上画控件边框 + id 标签
"""

import base64
import io
import logging
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger("deskhand.annotator")


def screenshot(window_rect: Optional[dict] = None) -> Image.Image:
    """
    截取屏幕或指定窗口区域。

    window_rect: {"left", "top", "right", "bottom"} 或 None（全屏）
    """
    from PIL import ImageGrab
    if window_rect:
        bbox = (
            window_rect["left"],
            window_rect["top"],
            window_rect["right"],
            window_rect["bottom"],
        )
        img = ImageGrab.grab(bbox=bbox)
    else:
        img = ImageGrab.grab()
    return img


def _collect_rects(node: dict, out: list) -> None:
    """递归收集所有带 rect 的控件节点。"""
    rect = node.get("rect")
    if rect and all(k in rect for k in ("left", "top", "right", "bottom")):
        out.append({
            "id": node["id"],
            "role": node.get("role", ""),
            "name": node.get("name", ""),
            "left": rect["left"],
            "top": rect["top"],
            "right": rect["right"],
            "bottom": rect["bottom"],
        })
    for child in node.get("children", []):
        _collect_rects(child, out)


def annotate(img: Image.Image, tree_state: dict,
             font_size: int = 12) -> Image.Image:
    """
    在截图上绘制控件边框和 id 标签。

    返回新的 Image 对象（不修改原图）。
    """
    img = img.copy()
    draw = ImageDraw.Draw(img)

    # 尝试加载字体
    try:
        font = ImageFont.truetype("arial.ttf", font_size)
    except Exception:
        font = ImageFont.load_default()

    rects = []
    _collect_rects(tree_state, rects)

    # 颜色池
    colors = [
        "#FF0000", "#00FF00", "#0000FF", "#FF00FF",
        "#00FFFF", "#FFFF00", "#FF8800", "#8800FF",
    ]

    for i, item in enumerate(rects):
        color = colors[i % len(colors)]
        bbox = (item["left"], item["top"], item["right"], item["bottom"])
        draw.rectangle(bbox, outline=color, width=2)

        # 标签文字
        label = f"[{item['id']}] {item['role']}"
        try:
            text_bbox = draw.textbbox((0, 0), label, font=font)
            tw, th = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]
        except Exception:
            tw, th = len(label) * font_size // 2, font_size

        tx = max(item["left"], 0)
        ty = max(item["top"] - th - 2, 0)
        # 标签背景
        draw.rectangle([tx, ty, tx + tw + 4, ty + th + 2], fill=color)
        draw.text((tx + 2, ty), label, fill="white", font=font)

    return img


def img_to_base64(img: Image.Image, fmt: str = "PNG") -> str:
    """将 PIL Image 转为 base64 字符串。"""
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/{fmt.lower()};base64,{b64}"
