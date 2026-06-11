"""
desk_select.py — Tool: 选中文本范围。
"""

import json
import logging

from ..engine.actuator import select_text

logger = logging.getLogger("deskhand.tools.select")


def desk_select(id: int, start: int, end: int) -> str:
    """
    选中指定控件内第 start 到第 end 个字符。

    :param id: 控件 id
    :param start: 起始字符位置（0-based）
    :param end: 结束字符位置（0-based，不包含）
    """
    try:
        result = select_text(id, start=start, end=end)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
