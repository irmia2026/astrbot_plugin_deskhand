"""
desk_scroll.py — Tool: 对指定控件滚轮。
"""

import json
import logging

from ..engine.actuator import scroll

logger = logging.getLogger("deskhand.tools.scroll")


def desk_scroll(id: int, direction: str, amount: int = 3) -> str:
    """
    对指定控件滚动。

    :param id: 控件 id
    :param direction: up/down/left/right
    :param amount: 滚动量，默认 3
    """
    try:
        result = scroll(id, direction=direction, amount=amount)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
