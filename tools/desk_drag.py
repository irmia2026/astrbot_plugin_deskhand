"""
desk_drag.py — Tool: 拖拽（id→id 或 id→坐标）。
"""

import json
import logging
from typing import Optional

from ..engine.actuator import drag

logger = logging.getLogger("deskhand.tools.drag")


def desk_drag(from_id: int, to_id: Optional[int] = None,
              to_x: Optional[int] = None, to_y: Optional[int] = None) -> str:
    """
    拖拽操作。

    :param from_id: 起始控件 id
    :param to_id: 目标控件 id（与 to_x/to_y 二选一）
    :param to_x: 目标 x 坐标
    :param to_y: 目标 y 坐标
    """
    try:
        result = drag(from_id, to_id=to_id, to_x=to_x, to_y=to_y)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
