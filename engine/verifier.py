"""
verifier.py — 操作后多维信号自验证框架。

零新增 pip 依赖。所有信号采集用已依赖的 win32gui / win32api / uiautomation / PIL。

核心函数:
- capture_signals(target_rect=None) → dict
- compare_signals(before, after) → dict
"""

import io
import time
import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger("deskhand.verifier")

# 像素对比阈值（灰度值差异超过此值计为变化像素）
_DIFF_THRESHOLD = 5


# ── 信号采集 ────────────────────────────────────────────────────

def capture_signals(target_rect: Optional[tuple] = None) -> dict:
    """
    采集多维零成本/低成本信号。

    target_rect: (left, top, right, bottom) 或 None。
                 仅在提供时才截取该区域截图。
    所有异常静默捕获，errors 列表记录失败项。
    """
    sig = {"errors": []}

    # 1. 前台窗口句柄 + 标题
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        sig["foreground_hwnd"] = hwnd if hwnd else None
        if hwnd:
            try:
                sig["foreground_title"] = win32gui.GetWindowText(hwnd)
            except Exception:
                sig["foreground_title"] = None
                sig["errors"].append("foreground_title")
        else:
            sig["foreground_title"] = None
    except Exception:
        sig["foreground_hwnd"] = None
        sig["foreground_title"] = None
        sig["errors"].append("foreground")

    # 2. 光标位置
    try:
        import win32api
        pt = win32api.GetCursorPos()
        sig["cursor_x"] = pt[0]
        sig["cursor_y"] = pt[1]
    except Exception:
        sig["cursor_x"] = None
        sig["cursor_y"] = None
        sig["errors"].append("cursor")

    # 3. UIA 控件树快照（depth=2，仅顶层+直接子节点）
    try:
        uia_snap = _light_uia_snapshot()
        sig["uia_root_id"] = uia_snap.get("root_id")
        sig["uia_key_controls"] = uia_snap.get("key_controls", [])
    except Exception:
        sig["uia_root_id"] = None
        sig["uia_key_controls"] = None
        sig["errors"].append("uia_snapshot")

    # 4. 目标区域截图（仅在 target_rect 提供且有效时）
    if target_rect is not None:
        if _is_valid_rect(target_rect):
            try:
                from PIL import ImageGrab
                img = ImageGrab.grab(bbox=target_rect)
                try:
                    sig["screenshot_bytes"] = _img_to_bytes(img)
                finally:
                    img.close()
            except Exception:
                sig["screenshot_bytes"] = None
                sig["errors"].append("screenshot")
        else:
            sig["screenshot_bytes"] = None
            sig["errors"].append("screenshot_invalid_rect")
    else:
        sig["screenshot_bytes"] = None

    # 5. 时间戳
    sig["ts"] = time.perf_counter()

    return sig


# ── 轻量 UIA 扫描（depth=2）────────────────────────────────────

def _light_uia_snapshot() -> dict:
    """轻量 UIA 扫描：仅采集活跃窗口 + 直接子节点（depth≤2）。"""
    try:
        import uiautomation as uia
        root = uia.GetRootControl()
        focused = uia.GetFocusedControl()
        if focused is None:
            children = root.GetChildren()
            focused = children[0] if children else None
        if focused is None:
            return {"root_id": None, "key_controls": []}

        # 找到顶层窗口
        walk = focused
        while walk:
            try:
                parent = walk.GetParentControl()
                if parent is None or parent == root:
                    break
                walk = parent
            except Exception:
                break
        top_window = walk

        # 获取 RuntimeId（用 hash 做轻量标识，不与 cache 绑定）
        try:
            rt = top_window.GetRuntimeId()
            root_id = hash(tuple(rt)) if rt else None
        except Exception:
            root_id = None

        # 采集子节点（仅一层）
        key_controls = []
        try:
            children = top_window.GetChildren()
            if children:
                for child in children[:30]:
                    item = _summarize_control(child)
                    if item:
                        key_controls.append(item)
        except Exception:
            pass

        return {"root_id": root_id, "key_controls": key_controls}
    except Exception:
        return {"root_id": None, "key_controls": None}


def _summarize_control(control) -> Optional[dict]:
    """提取控件关键摘要：role / name / value / has_focus / rect。"""
    try:
        item = {}
        item["role"] = getattr(control, "ControlTypeName", "Unknown") or "Unknown"
        item["name"] = (getattr(control, "Name", "") or "")[:40]
        item["enabled"] = bool(getattr(control, "IsEnabled", True))
        try:
            item["has_focus"] = bool(getattr(control, "HasKeyboardFocus", False))
        except Exception:
            item["has_focus"] = False
        # 尝试获取值（编辑控件）
        if item["role"] in ("EditControl", "DocumentControl", "ComboBoxControl"):
            try:
                import uiautomation as uia
                vp = control.GetPattern(uia.PatternId.ValuePattern)
                if vp:
                    item["value"] = (vp.CurrentValue or "")[:100]
            except Exception:
                pass
        bb = getattr(control, "BoundingRectangle", None)
        if bb:
            item["rect"] = {
                "left": int(bb.left), "top": int(bb.top),
                "right": int(bb.right), "bottom": int(bb.bottom),
            }
        return item
    except Exception:
        return None


# ── 信号对比 ────────────────────────────────────────────────────

def compare_signals(before: dict, after: dict) -> dict:
    """
    计算操作前后信号差异。

    返回结构化 dict，LLM 可直接推理。
    包含 errors 字段汇总采集失败项。
    """
    diff = {"errors": []}

    # 汇总采集错误
    diff["errors"].extend(before.get("errors", []))
    diff["errors"].extend(after.get("errors", []))
    diff["errors"] = list(set(diff["errors"]))  # 去重

    # 前台窗口变化
    diff["foreground_changed"] = (before.get("foreground_hwnd") != after.get("foreground_hwnd"))
    diff["foreground_hwnd_before"] = _fmt_hwnd(before.get("foreground_hwnd"))
    diff["foreground_hwnd_after"] = _fmt_hwnd(after.get("foreground_hwnd"))

    # 窗口标题变化
    title_before = before.get("foreground_title", "")
    title_after = after.get("foreground_title", "")
    diff["title_changed"] = (title_before != title_after)
    diff["title_before"] = title_before
    diff["title_after"] = title_after

    # 光标移动
    cx_b = before.get("cursor_x")
    cy_b = before.get("cursor_y")
    cx_a = after.get("cursor_x")
    cy_a = after.get("cursor_y")
    if cx_b is not None and cx_a is not None and cy_b is not None and cy_a is not None:
        dx = cx_a - cx_b
        dy = cy_a - cy_b
        diff["cursor_moved"] = (dx != 0 or dy != 0)
        diff["cursor_delta_x"] = dx
        diff["cursor_delta_y"] = dy
    else:
        diff["cursor_moved"] = None
        diff["cursor_delta_x"] = None
        diff["cursor_delta_y"] = None

    # 控件焦点变化
    focus_before = _get_focused_state(before.get("uia_key_controls"))
    focus_after = _get_focused_state(after.get("uia_key_controls"))
    if focus_before is not None and focus_after is not None:
        diff["focus_changed"] = (focus_before != focus_after)
        diff["focus_before"] = focus_before
        diff["focus_after"] = focus_after
    else:
        diff["focus_changed"] = None
        diff["focus_before"] = None
        diff["focus_after"] = None

    # 控件值变化（从 uia_key_controls 提取）
    val_before = _get_first_value(before.get("uia_key_controls"))
    val_after = _get_first_value(after.get("uia_key_controls"))
    if val_before is not None or val_after is not None:
        diff["value_changed"] = (val_before != val_after)
        diff["value_before"] = val_before
        diff["value_after"] = val_after
    else:
        diff["value_changed"] = None

    # 像素变化
    img_b = _bytes_to_img(before.get("screenshot_bytes"))
    img_a = _bytes_to_img(after.get("screenshot_bytes"))
    if img_b is not None and img_a is not None:
        try:
            visual = _pixel_diff(img_b, img_a)
            diff["visual_changed"] = visual["changed"]
            diff["visual_diff_percent"] = visual["percent"]
            diff["visual_diff_region"] = visual["region"]
            diff["visual_size_changed"] = visual.get("size_changed", False)
        finally:
            img_b.close()
            img_a.close()
    else:
        diff["visual_changed"] = None
        diff["visual_diff_percent"] = None
        diff["visual_diff_region"] = None
        diff["visual_size_changed"] = None

    return diff


def _pixel_diff(img_before: Image.Image, img_after: Image.Image,
                threshold: int = _DIFF_THRESHOLD) -> dict:
    """两张图逐像素对比，返回变化统计。"""
    if img_before.size != img_after.size:
        return {"changed": True, "percent": None, "region": None, "size_changed": True}

    # 转为灰度图加速对比
    try:
        gb = img_before.convert("L")
        ga = img_after.convert("L")
    except Exception:
        return {"changed": None, "percent": None, "region": None, "size_changed": False}

    try:
        pb = gb.load()
        pa = ga.load()
        w, h = gb.size
        total = w * h

        changed = 0
        min_x, min_y, max_x, max_y = w, h, 0, 0

        for x in range(w):
            for y in range(h):
                if abs(pb[x, y] - pa[x, y]) > threshold:
                    changed += 1
                    if x < min_x:
                        min_x = x
                    if y < min_y:
                        min_y = y
                    if x > max_x:
                        max_x = x
                    if y > max_y:
                        max_y = y

        if changed == 0:
            return {"changed": False, "percent": 0.0, "region": None, "size_changed": False}

        percent = round(changed / total * 100, 2)
        # region 边界 +1 以包含最后一个变化像素
        region = {"left": min_x, "top": min_y, "right": max_x + 1, "bottom": max_y + 1}
        return {"changed": True, "percent": percent, "region": region, "size_changed": False}
    finally:
        gb.close()
        ga.close()


# ── 辅助 ────────────────────────────────────────────────────────

def _is_valid_rect(rect: tuple) -> bool:
    """检查矩形是否有效（left < right, top < bottom, 面积 > 0）。"""
    if rect is None or len(rect) != 4:
        return False
    left, top, right, bottom = rect
    return left < right and top < bottom and (right - left) > 0 and (bottom - top) > 0


def _img_to_bytes(img: Image.Image) -> Optional[bytes]:
    try:
        buf = io.BytesIO()
        try:
            img.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            buf.close()
    except Exception:
        return None


def _bytes_to_img(data) -> Optional[Image.Image]:
    if data is None:
        return None
    try:
        return Image.open(io.BytesIO(data))
    except Exception:
        return None


def _fmt_hwnd(hwnd) -> Optional[str]:
    if hwnd is None:
        return None
    return f"0x{hwnd:08X}"


def _get_focused_state(key_controls) -> Optional[dict]:
    """从 key_controls 中找获焦控件。None 表示采集失败。"""
    if key_controls is None:
        return None
    for ctrl in key_controls:
        if ctrl and ctrl.get("has_focus"):
            return {"role": ctrl.get("role"), "name": ctrl.get("name")}
    # 明确返回 "无焦点控件" 而非 None
    return {"role": None, "name": None, "status": "no_focus"}


def _get_first_value(key_controls) -> Optional[str]:
    """从 key_controls 中提取第一个有 value 的编辑控件文本。"""
    if key_controls is None:
        return None
    for ctrl in key_controls:
        if ctrl and ctrl.get("role") in ("EditControl", "DocumentControl"):
            return ctrl.get("value")
    return None
