"""
scanner.py — 控件树采集，UIA 后端自适应。

- 默认 backend="uia"，穿透 WebView2/Electron/Tauri 壳层
- 用 uiautomation.GetRootControl() 出发，只采集当前活跃窗口
- 递归遍历子树，深度限制 max_depth=15，子节点限制 max_children=120
- 超时保护：单窗口扫描 ≤5s
"""

import time
import logging
from typing import Optional

from engine.cache import get_global_cache, ControlCache

logger = logging.getLogger("deskhand.scanner")

# Chromium 渲染宿主 ClassName
_CHROMIUM_HOST_CLASSES = {
    "Chrome_RenderWidgetHostHWND",
    "Chrome_WidgetWin_0",
}

# 采集的字段
_CONTROL_FIELDS = ("id", "role", "name", "value", "rect", "enabled")

# 默认限制（可通过配置覆盖）
MAX_DEPTH = 15
MAX_CHILDREN = 120
SCAN_TIMEOUT_SEC = 5.0


def _get_config() -> dict:
    """尝试从 AstrBot 配置读取参数，失败返回空 dict。"""
    try:
        # AstrBot 配置通常通过环境或全局上下文获取
        # 这里尝试读取已知的配置路径
        import json
        import os
        config_path = os.environ.get("ASTRBOT_CONFIG_PATH", "")
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("astrbot_plugin_deskhand", {})
    except Exception:
        pass
    return {}


def _get_scan_limits() -> tuple[int, int, float]:
    """返回 (max_depth, max_children, scan_timeout_sec)。"""
    cfg = _get_config()
    return (
        cfg.get("max_depth", MAX_DEPTH),
        cfg.get("max_children", MAX_CHILDREN),
        cfg.get("scan_timeout", SCAN_TIMEOUT_SEC),
    )


def _is_chromium_host(control) -> bool:
    """检测是否为 Chromium 渲染宿主（WebView2/Electron/Chrome）。"""
    try:
        return control.ClassName in _CHROMIUM_HOST_CLASSES
    except Exception:
        return False


def _safe_get_children(control, max_children: int = MAX_CHILDREN) -> list:
    """安全获取子控件列表，处理 UIA 异常并限制数量。"""
    try:
        children = control.GetChildren()
        if children is None:
            return []
        # 某些 UIA 实现返回迭代器而非 list
        if hasattr(children, "__iter__") and not isinstance(children, list):
            children = list(children)
        return children[:max_children]
    except Exception as exc:
        logger.warning("GetChildren failed: %s", exc)
        return []


def _safe_attr(control, attr: str, default=""):
    """安全读取控件属性，避免 UIA 异常。"""
    try:
        val = getattr(control, attr, default)
        if val is None:
            return default
        # 截断过长的值
        if isinstance(val, str) and len(val) > 100:
            return val[:100]
        return val
    except Exception:
        return default


def _safe_rect(control) -> dict:
    """安全获取控件矩形坐标。"""
    try:
        bb = control.BoundingRectangle
        if bb:
            return {
                "left": int(bb.left),
                "top": int(bb.top),
                "right": int(bb.right),
                "bottom": int(bb.bottom),
                "width": int(bb.width()),
                "height": int(bb.height()),
            }
    except Exception:
        pass
    return {}


def scan_control(control, cache: ControlCache, depth: int = 0,
                 start_time: Optional[float] = None,
                 max_depth: int = MAX_DEPTH,
                 max_children: int = MAX_CHILDREN,
                 scan_timeout: float = SCAN_TIMEOUT_SEC) -> Optional[dict]:
    """
    递归采集单个控件及其子树。

    返回结构化 dict，超时或遇到不可遍历控件时返回 None（子树截断）。
    """
    if start_time is None:
        start_time = time.monotonic()

    if time.monotonic() - start_time > scan_timeout:
        logger.warning("Scan timeout at depth %d", depth)
        return None

    if depth > max_depth:
        return None

    try:
        # 获取 RuntimeId 用于稳定映射
        rt_raw = control.GetRuntimeId()
        runtime_id = tuple(rt_raw) if rt_raw else None
    except Exception:
        runtime_id = None

    if runtime_id is None:
        return None

    cid = cache.get_id(runtime_id)

    # 采集基础属性
    role = _safe_attr(control, "ControlTypeName", "Unknown")
    name = _safe_attr(control, "Name")
    enabled = _safe_attr(control, "IsEnabled", True)
    value = ""
    if role in ("EditControl", "DocumentControl", "ComboBoxControl"):
        # 尝试通过 ValuePattern 获取文本
        try:
            import uiautomation as uia
            vp = control.GetPattern(uia.PatternId.ValuePattern)
            if vp:
                val = vp.CurrentValue
                value = val[:100] if val else ""
        except Exception:
            pass

    node = {
        "id": cid,
        "role": role,
        "name": name,
        "value": value,
        "rect": _safe_rect(control),
        "enabled": bool(enabled) if enabled is not None else True,
        "children": [],
    }

    # 递归采集子控件 — 即使是 Chromium 宿主也正常递归
    children = _safe_get_children(control, max_children)
    for child in children:
        if len(node["children"]) >= max_children:
            logger.debug("Max children (%d) reached at depth %d", max_children, depth)
            break
        child_node = scan_control(child, cache, depth + 1, start_time,
                                  max_depth, max_children, scan_timeout)
        if child_node is not None:
            node["children"].append(child_node)

    return node


def scan_active_window(cache: Optional[ControlCache] = None) -> Optional[dict]:
    """
    采集当前活跃窗口的控件树。

    返回结构化 dict，根节点为窗口信息。
    """
    if cache is None:
        cache = get_global_cache()

    import uiautomation as uia

    max_depth, max_children, scan_timeout = _get_scan_limits()
    start_time = time.monotonic()

    try:
        root = uia.GetRootControl()
        if root is None:
            logger.error("GetRootControl() returned None")
            return None
    except Exception as exc:
        logger.error("GetRootControl() failed: %s", exc)
        return None

    # 找到活跃窗口
    try:
        active_control = uia.GetFocusedControl()
        if active_control is None:
            # fallback: 拿第一个顶层窗口
            windows = root.GetChildren()
            if windows:
                active_control = windows[0]
            else:
                logger.error("No window found")
                return None

        # 确保我们拿到的是顶层窗口（而不是内部焦点控件）
        # 沿着祖先链找到顶层窗口
        walk = active_control
        while walk:
            try:
                parent = walk.GetParentControl()
                if parent is None or parent == root:
                    break
                walk = parent
            except Exception:
                break
        active_window = walk
    except Exception as exc:
        logger.error("Failed to find active window: %s", exc)
        return None

    # 采集窗口控件树
    window_node = scan_control(active_window, cache, 0, start_time,
                               max_depth, max_children, scan_timeout)
    if window_node is None:
        logger.error("Failed to scan active window")
        return None

    elapsed = time.monotonic() - start_time
    logger.info("Scanned active window in %.2fs, root id=%d role=%s",
                elapsed, window_node["id"], window_node["role"])

    return window_node
