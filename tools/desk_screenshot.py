"""
desk_screenshot.py — Tool: 截图（可选标注控件框）。
"""

import json
import logging
from typing import Optional

from engine.annotator import screenshot, annotate, img_to_base64
from engine.scanner import scan_active_window
from engine.cache import get_global_cache

logger = logging.getLogger("deskhand.tools.screenshot")


def desk_screenshot(annotate: bool = False) -> str:
    """
    截取当前屏幕或活跃窗口，返回 base64 PNG。

    :param annotate: 是否在截图上标注控件边框和 id，默认 False
    """
    try:
        cache = get_global_cache()
        tree = scan_active_window(cache)
        if tree is None:
            # 无法获取控件树，截全屏
            img = screenshot()
            return json.dumps({
                "success": True,
                "annotated": False,
                "image": img_to_base64(img),
            }, ensure_ascii=False)

        # 用窗口 rect 截图
        rect = tree.get("rect")
        if rect:
            img = screenshot(rect)
        else:
            img = screenshot()

        if annotate and tree:
            img = annotate(img, tree)

        return json.dumps({
            "success": True,
            "annotated": annotate,
            "image": img_to_base64(img),
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
