"""
desk_state.py — Tool: 采集活跃窗口控件树。
"""

import json
import logging

from ..engine.scanner import scan_active_window
from ..engine.cache import get_global_cache

logger = logging.getLogger("deskhand.tools.state")


def desk_state() -> str:
    """
    采集当前活跃窗口的控件树，返回 JSON 字符串。

    包含 id / role / name / value / rect / enabled / children。
    控件 id 在多次调用间保持稳定。
    """
    cache = get_global_cache()
    tree = scan_active_window(cache)
    if tree is None:
        return json.dumps({"error": "无法采集控件树"}, ensure_ascii=False)

    # 截断输出以控制 token 长度
    def _truncate(node: dict, depth: int = 0) -> dict:
        """截断 value 长度，限制深度输出。"""
        out = {
            "id": node["id"],
            "role": node["role"],
            "name": node["name"][:50] if node["name"] else "",
            "value": node["value"][:50] if node["value"] else "",
            "enabled": node["enabled"],
        }
        if node.get("rect"):
            out["rect"] = node["rect"]
        if depth < 3 and node.get("children"):
            out["children"] = [_truncate(c, depth + 1) for c in node["children"]]
        elif node.get("children"):
            out["children_count"] = len(node["children"])
        return out

    truncated = _truncate(tree)
    return json.dumps(truncated, ensure_ascii=False, indent=2)
