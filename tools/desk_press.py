"""
desk_press.py — Tool: 快捷键组合。
"""

import json
import logging
from typing import Optional

from ..engine.actuator import press

logger = logging.getLogger("deskhand.tools.press")


def desk_press(keys: list[str], action: str = "press") -> str:
    """
    发送键盘按键。

    :param keys: 按键列表，如 ["ctrl", "a"] 或 ["enter"]
    :param action: press/key_down/key_up，默认 press
    """
    try:
        result = press(keys, action=action)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
