"""
desk_window.py — Tool: 窗口管理（最小化/最大化/恢复/关闭/置顶/移动/调整大小）。
"""

import json
import logging
from typing import Optional

from ..engine.actuator import window_action

logger = logging.getLogger("deskhand.tools.window")


def desk_window(action: str, hwnd: Optional[int] = None,
                x: Optional[int] = None, y: Optional[int] = None,
                w: Optional[int] = None, h: Optional[int] = None,
                verify: str = "full") -> str:
    """
    窗口管理操作。

    :param action: min/max/restore/close/focus/set_topmost/unset_topmost/move/resize
    :param hwnd: 窗口句柄，None 则操作当前活跃窗口
    :param x, y: move 用坐标
    :param w, h: resize 用宽高
    :param verify: full截屏对比/light仅前景/none跳过
    """
    try:
        result = window_action(action, hwnd=hwnd, x=x, y=y, w=w, h=h, verify=verify)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
