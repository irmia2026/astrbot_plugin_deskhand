"""
desk_type.py — Tool: 输入文本 / 清空指定行。
"""

import json
import logging
from typing import Optional

from ..engine.actuator import type_text

logger = logging.getLogger("deskhand.tools.type")


def desk_type(id: int, text: str, line: Optional[int] = None) -> str:
    """
    向控件输入文本。

    :param id: 控件 id（来自 desk_state）
    :param text: 要输入的文本
    :param line: 指定行号（1-based），修改该行内容；None 则直接输入
    """
    try:
        result = type_text(id, text=text, line=line)
        return json.dumps(result, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
