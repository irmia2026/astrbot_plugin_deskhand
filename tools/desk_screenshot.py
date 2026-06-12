"""
desk_screenshot.py — Tool: 截图（可选标注控件框），保存为 PNG 文件，返回路径。
"""

import json
import os
import time
import logging

from PIL import Image

from ..engine.annotator import screenshot, annotate as _annotate
from ..engine.scanner import scan_active_window
from ..engine.cache import get_global_cache

logger = logging.getLogger("deskhand.tools.screenshot")

# 截图保存目录
_SCREENSHOT_DIR = os.path.join(os.path.expanduser("~"), ".astrbot", "data", "screenshots")


def desk_screenshot(annotate: bool = False) -> str:
    """
    截取当前屏幕，保存为 PNG 文件，返回文件路径。

    :param annotate: 是否在截图上标注控件边框和 id，默认 False
    """
    try:
        os.makedirs(_SCREENSHOT_DIR, exist_ok=True)

        cache = get_global_cache()
        tree = scan_active_window(cache)

        # 用窗口 rect 截图
        if tree and tree.get("rect"):
            img = screenshot(tree["rect"])
        else:
            img = screenshot()

        if annotate and tree:
            img = _annotate(img, tree)

        # 保存为 PNG 文件
        ts = time.strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{ts}.png"
        filepath = os.path.join(_SCREENSHOT_DIR, filename)
        try:
            img.save(filepath, "PNG")
        finally:
            img.close()

        size_kb = os.path.getsize(filepath) // 1024

        return json.dumps({
            "success": True,
            "annotated": annotate,
            "file": filepath,
            "size_kb": size_kb,
            "resolution": f"{img.width}x{img.height}",
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
