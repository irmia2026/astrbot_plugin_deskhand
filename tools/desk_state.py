"""
desk_state.py — Tool: 采集控件树。
"""

import json
import logging
from typing import Optional

from ..engine.scanner import scan_active_window
from ..engine.cache import get_global_cache

logger = logging.getLogger("deskhand.tools.state")


def desk_state(target: Optional[str] = None) -> str:
    """
    采集控件树。

    target=None 时扫描全桌面（当前活跃窗口）。
    target="QQ" 时只扫描名称包含 "QQ" 的窗口（模糊匹配，不区分大小写）。
    """
    cache = get_global_cache()
    tree = scan_active_window(cache, target=target)
    if tree is None:
        msg = f"未找到匹配 '{target}' 的窗口" if target else "无法采集控件树"
        return json.dumps({"error": msg}, ensure_ascii=False)

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
