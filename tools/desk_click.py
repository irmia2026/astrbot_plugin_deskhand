"""
desk_click.py — Tool: 点击（左/右/中/双击）或悬停。
"""

import json
import logging

from ..engine.actuator import click

logger = logging.getLogger("deskhand.tools.click")


def desk_click(id: int, button: str = "left", double: bool = False,
               hover: bool = False) -> str:
    """
    点击或悬停指定控件。

    :param id: 控件 id（来自 desk_state）
    :param button: left/right/middle，默认 left
    :param double: 是否双击，默认 False
    :param hover: 是否仅悬停（移动鼠标不点击），默认 False
    """
    try:
        result = click(id, button=button, double=double, hover=hover)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
