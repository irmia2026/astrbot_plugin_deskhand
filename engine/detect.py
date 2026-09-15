"""
detect.py — 纯 CV 候选框检测（OpenCV，可选依赖）。

定位「任何能框选标定的东西」：边缘检测 → 轮廓提取 → 几何过滤，
输出候选交互区域的精确矩形。没有语义（不知道这是按钮还是图片），
但保证「凡是有边框/有区块的东西都被框出来」——配合 OCR 文字（自带语义）
和 VL（语义仲裁）形成完整覆盖。

无 OpenCV 时 available() 返回 False，调用方自动跳过本通道。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("deskhand.detect")

_available: Optional[bool] = None


def available() -> bool:
    global _available
    if _available is None:
        try:
            import cv2  # noqa: F401

            _available = True
        except ImportError:
            _available = False
            logger.info("未安装 opencv-python-headless，CV 候选框通道关闭")
    return _available


def detect_boxes(image, max_boxes: int = 40) -> list[dict]:
    """检测图像中的候选交互框，返回 [{x, y, left, top, right, bottom, area}]（中心点+矩形）。

    过滤规则（面向 UI/游戏画面调参）：
    - 面积：≥ 0.02% 且 ≤ 30% 画面（太小是噪点，太大是背景面板）；
    - 长宽比：0.15 ~ 6（细长线和极端方块都不要）；
    - 去重：中心距 < 12px 的框合并取大者。
    """
    if not available():
        return []
    import cv2
    import numpy as np

    arr = np.array(image.convert("RGB"))
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    w, h = image.size
    img_area = w * h

    # 边缘 + 形态学闭合，把断开的边框连成完整轮廓
    edges = cv2.Canny(gray, 40, 120)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)

    contours, _ = cv2.findContours(closed, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

    boxes: list[dict] = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        area = bw * bh
        if area < img_area * 0.0002 or area > img_area * 0.30:
            continue
        if bw < 14 or bh < 10:
            continue
        ratio = bw / max(bh, 1)
        if ratio < 0.15 or ratio > 6.0:
            continue
        boxes.append(
            {
                "x": x + bw // 2,
                "y": y + bh // 2,
                "left": x, "top": y, "right": x + bw, "bottom": y + bh,
                "area": area,
            }
        )

    # 中心距去重（同一元素的内外双层框取大者）
    boxes.sort(key=lambda b: -b["area"])
    kept: list[dict] = []
    for b in boxes:
        if all(abs(b["x"] - k["x"]) >= 12 or abs(b["y"] - k["y"]) >= 12 for k in kept):
            kept.append(b)
        if len(kept) >= max_boxes:
            break
    return kept
