"""
actuator.py — 执行器：click / type / press / drag / scroll / select / window。

所有操作前做 precheck：控件 handle 仍有效、仍 enabled。
底层通过 uiautomation 的 UIA 后端交互，fallback 到 win32api 鼠标事件。

支持 verify=True（默认），操作后自动采集多维信号并计算前后差异，
在返回值中附加 _verify 字段供 LLM 推理操作是否生效。
"""

import time
import logging
from typing import Optional

from .cache import get_global_cache, ControlCache
from .verifier import capture_signals, compare_signals

logger = logging.getLogger("deskhand.actuator")

# verify 后等待时长（秒），让 GUI 反映变化
_VERIFY_WAIT = 0.2


# ── 辅助：从 cache 查控件 ──────────────────────────────────────

def _get_control_by_id(cid: int, cache: Optional[ControlCache] = None):
    """根据控件 id 从缓存获取 UIA 控件对象（含有效性验证）。"""
    if cache is None:
        cache = get_global_cache()
    return cache.get_control(cid)


def _precheck(control) -> None:
    """操作前检查：控件存在且 enabled。"""
    try:
        if not control.Exists(0.5):
            raise RuntimeError("控件不存在或已销毁")
    except Exception as exc:
        raise RuntimeError(f"precheck 失败: {exc}")

    try:
        if not control.IsEnabled:
            raise RuntimeError("控件已禁用（disabled）")
    except Exception as exc:
        raise RuntimeError(f"precheck 失败: {exc}")


def _clickable_point(control) -> tuple[int, int]:
    """获取控件的可点击坐标。"""
    # 首选 BoundingRectangle 中心（比 GetClickablePoint 更可靠）
    try:
        bb = control.BoundingRectangle
        if bb and (bb.right - bb.left) > 0 and (bb.bottom - bb.top) > 0:
            return (
                int((bb.left + bb.right) / 2),
                int((bb.top + bb.bottom) / 2),
            )
    except Exception:
        pass
    try:
        pt = control.GetClickablePoint()
        if pt and len(pt) >= 2:
            return (int(pt[0]), int(pt[1]))
    except Exception:
        pass
    raise RuntimeError("无法获取控件可点击坐标")


def _control_rect(control) -> Optional[tuple]:
    """返回控件的 PIL 兼容矩形 (left, top, right, bottom)。"""
    try:
        bb = control.BoundingRectangle
        if bb:
            return (int(bb.left), int(bb.top), int(bb.right), int(bb.bottom))
    except Exception:
        pass
    return None


def _active_window_rect() -> Optional[tuple]:
    """返回当前活跃窗口的矩形。"""
    try:
        import win32gui
        hwnd = win32gui.GetForegroundWindow()
        if hwnd:
            return win32gui.GetWindowRect(hwnd)
    except Exception:
        pass
    return None


def _window_rect_by_hwnd(hwnd: int) -> Optional[tuple]:
    """返回指定 HWND 的窗口矩形。"""
    try:
        import win32gui
        return win32gui.GetWindowRect(hwnd)
    except Exception:
        return None


# ── 鼠标事件底层 ───────────────────────────────────────────────

def _mouse_move(x: int, y: int) -> None:
    import win32api
    import win32con
    win32api.SetCursorPos((x, y))


def _mouse_click(x: int, y: int, button: str = "left", double: bool = False) -> None:
    import win32api
    import win32con
    _mouse_move(x, y)
    time.sleep(0.05)

    btn_map = {
        "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
        "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
        "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
    }
    down, up = btn_map.get(button, btn_map["left"])

    times = 2 if double else 1
    for _ in range(times):
        win32api.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.05)
        win32api.mouse_event(up, 0, 0, 0, 0)
        if double:
            time.sleep(0.05)


def _mouse_down(x: int, y: int, button: str = "left") -> None:
    import win32api
    import win32con
    _mouse_move(x, y)
    time.sleep(0.05)
    btn_map = {
        "left": win32con.MOUSEEVENTF_LEFTDOWN,
        "right": win32con.MOUSEEVENTF_RIGHTDOWN,
        "middle": win32con.MOUSEEVENTF_MIDDLEDOWN,
    }
    win32api.mouse_event(btn_map.get(button, win32con.MOUSEEVENTF_LEFTDOWN), 0, 0, 0, 0)


def _mouse_up(x: int, y: int, button: str = "left") -> None:
    import win32api
    import win32con
    _mouse_move(x, y)
    time.sleep(0.05)
    btn_map = {
        "left": win32con.MOUSEEVENTF_LEFTUP,
        "right": win32con.MOUSEEVENTF_RIGHTUP,
        "middle": win32con.MOUSEEVENTF_MIDDLEUP,
    }
    win32api.mouse_event(btn_map.get(button, win32con.MOUSEEVENTF_LEFTUP), 0, 0, 0, 0)


def _mouse_wheel(x: int, y: int, amount: int) -> None:
    import win32api
    import win32con
    _mouse_move(x, y)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, amount, 0)


# ── 失败时 _verify 的标准格式 ──────────────────────────────────

def _verify_failure(error_msg: str) -> dict:
    """操作失败时返回与成功时相同 schema 的 _verify，增加 success=false。"""
    return {
        "success": False,
        "error": error_msg,
        "foreground_changed": None,
        "foreground_hwnd_before": None,
        "foreground_hwnd_after": None,
        "title_changed": None,
        "title_before": None,
        "title_after": None,
        "cursor_moved": None,
        "cursor_delta_x": None,
        "cursor_delta_y": None,
        "focus_changed": None,
        "focus_before": None,
        "focus_after": None,
        "value_changed": None,
        "value_before": None,
        "value_after": None,
        "visual_changed": None,
        "visual_diff_percent": None,
        "visual_diff_region": None,
        "visual_size_changed": None,
        "errors": ["operation_failed"],
    }


# ── 公开 API ───────────────────────────────────────────────────

def click(cid: int, button: str = "left", double: bool = False,
          hover: bool = False, verify: str = "full",
          cache: Optional[ControlCache] = None) -> dict:
    """点击/悬停指定控件。verify=full/light/none。light跳过像素diff但保留foreground。"""
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    do_screenshot = (verify == "full")
    if verify != "none":
        target_rect = _control_rect(control) if do_screenshot else None
        before = capture_signals(target_rect=target_rect)

    x, y = _clickable_point(control)

    if hover:
        _mouse_move(x, y)
        result = {"success": True, "action": "hover", "id": cid, "x": x, "y": y}
    else:
        _mouse_click(x, y, button, double)
        result = {"success": True, "action": "click", "id": cid, "button": button,
                  "double": double, "x": x, "y": y}

    if verify != "none":
        time.sleep(_VERIFY_WAIT)
        target_rect2 = _control_rect(control) if do_screenshot else None
        after = capture_signals(target_rect=target_rect2)
        result["_verify"] = compare_signals(before, after)

    return result


def drag(from_id: int, to_id: Optional[int] = None,
         to_x: Optional[int] = None, to_y: Optional[int] = None,
         verify: str = "full", cache: Optional[ControlCache] = None) -> dict:
    """拖拽：从控件 A 拖到控件 B 或指定坐标。信号: foreground, cursor, visual。"""
    from_control = _get_control_by_id(from_id, cache)
    _precheck(from_control)

    if to_id is not None:
        to_control = _get_control_by_id(to_id, cache)
        _precheck(to_control)

    # 确定目标区域（用于验证截图）
    if verify != "none":
        rects = [_control_rect(from_control)]
        if to_id is not None:
            rects.append(_control_rect(to_control))
        target_rect = _union_rects(rects) if any(rects) else None
        before = capture_signals(target_rect=target_rect)

    x1, y1 = _clickable_point(from_control)

    if to_id is not None:
        x2, y2 = _clickable_point(to_control)
    elif to_x is not None and to_y is not None:
        x2, y2 = to_x, to_y
    else:
        raise RuntimeError("drag 必须指定 to_id 或 to_x+to_y")

    _mouse_down(x1, y1)
    time.sleep(0.1)
    steps = 5
    for i in range(1, steps + 1):
        mx = int(x1 + (x2 - x1) * i / steps)
        my = int(y1 + (y2 - y1) * i / steps)
        _mouse_move(mx, my)
        time.sleep(0.02)
    _mouse_up(x2, y2)

    result = {"success": True, "action": "drag", "from_id": from_id,
              "to_id": to_id, "to_x": x2, "to_y": y2}

    if verify != "none":
        time.sleep(_VERIFY_WAIT)
        after = capture_signals(target_rect=target_rect)
        result["_verify"] = compare_signals(before, after)

    return result


def type_text(cid: int, text: str, line: Optional[int] = None,
              verify: str = "full", cache: Optional[ControlCache] = None) -> dict:
    """向控件输入文本。信号: foreground, title, visual, 控件值。"""
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    if verify != "none":
        target_rect = _control_rect(control)
        before = capture_signals(target_rect=target_rect)

    if line is not None:
        # 先获取当前值，修改指定行
        try:
            import uiautomation as uia
            vp = control.GetPattern(uia.PatternId.ValuePattern)
            if vp:
                current = vp.CurrentValue or ""
                lines = current.split("\n")
                idx = line - 1
                if 0 <= idx < len(lines):
                    lines[idx] = text
                elif idx >= len(lines):
                    # 补空行
                    lines.extend([""] * (idx - len(lines) + 1))
                    lines[idx] = text
                new_val = "\n".join(lines)
                vp.SetValue(new_val)
                result = {"success": True, "action": "type", "id": cid,
                          "line": line, "text": text, "lines_affected": len(lines)}
                if verify != "none":
                    time.sleep(_VERIFY_WAIT)
                    after = capture_signals(target_rect=target_rect)
                    result["_verify"] = compare_signals(before, after)
                return result
        except Exception as exc:
            logger.warning("SetValue line-edit failed: %s", exc)

    # fallback: SendKeys
    import uiautomation as uia
    try:
        if line is not None:
            # 先清空再输入
            control.SendKeys("{Ctrl}a{Delete}")
            time.sleep(0.05)
        safe_text = text.replace("{", "{{").replace("}", "}}")
        safe_text = safe_text.replace("\n", "{Enter}").replace("\t", "{Tab}")
        control.SendKeys(safe_text)
        result = {"success": True, "action": "type", "id": cid,
                  "text": text, "method": "SendKeys"}
    except Exception as exc:
        result = {"success": False, "error": str(exc)}
        if verify != "none":
            result["_verify"] = _verify_failure(str(exc))
        return result

    if verify != "none":
        time.sleep(_VERIFY_WAIT)
        after = capture_signals(target_rect=target_rect)
        result["_verify"] = compare_signals(before, after)

    return result


def press(keys: list[str], action: str = "press",
          verify: str = "full") -> dict:
    """发送键盘按键。信号: foreground, title, visual(活跃窗口区域)。"""
    if verify != "none":
        target_rect = _active_window_rect()
        before = capture_signals(target_rect=target_rect)

    import uiautomation as uia
    import win32api
    import win32con

    vk_map = {
        "ctrl": win32con.VK_CONTROL, "alt": win32con.VK_MENU,
        "shift": win32con.VK_SHIFT, "win": win32con.VK_LWIN,
        "enter": win32con.VK_RETURN, "return": win32con.VK_RETURN,
        "tab": win32con.VK_TAB, "esc": win32con.VK_ESCAPE,
        "escape": win32con.VK_ESCAPE, "space": win32con.VK_SPACE,
        "backspace": win32con.VK_BACK, "delete": win32con.VK_DELETE,
        "up": win32con.VK_UP, "down": win32con.VK_DOWN,
        "left": win32con.VK_LEFT, "right": win32con.VK_RIGHT,
        "home": win32con.VK_HOME, "end": win32con.VK_END,
        "pageup": win32con.VK_PRIOR, "pagedown": win32con.VK_NEXT,
        "f1": win32con.VK_F1, "f2": win32con.VK_F2,
        "f3": win32con.VK_F3, "f4": win32con.VK_F4,
        "f5": win32con.VK_F5, "f6": win32con.VK_F6,
        "f7": win32con.VK_F7, "f8": win32con.VK_F8,
        "f9": win32con.VK_F9, "f10": win32con.VK_F10,
        "f11": win32con.VK_F11, "f12": win32con.VK_F12,
    }

    modifier_vks = []
    normal_vks = []
    for k in keys:
        kl = k.lower()
        vk = vk_map.get(kl)
        if vk is None:
            if len(k) == 1:
                vk = win32api.VkKeyScan(k)
                if vk != -1:
                    vk = vk & 0xFF
                else:
                    vk = ord(k.upper())
            else:
                result = {"success": False, "error": f"未知按键: {k}"}
                if verify != "none":
                    result["_verify"] = _verify_failure(f"未知按键: {k}")
                return result
        if kl in ("ctrl", "alt", "shift", "win"):
            modifier_vks.append(vk)
        else:
            normal_vks.append(vk)

    def _key_event(vk: int, flags: int = 0) -> None:
        win32api.keybd_event(vk, 0, flags, 0)

    try:
        if action == "press":
            for vk in modifier_vks:
                _key_event(vk, 0)
                time.sleep(0.02)
            for vk in normal_vks:
                _key_event(vk, 0)
                time.sleep(0.02)
                _key_event(vk, win32con.KEYEVENTF_KEYUP)
                time.sleep(0.02)
            for vk in reversed(modifier_vks):
                _key_event(vk, win32con.KEYEVENTF_KEYUP)
                time.sleep(0.02)
            result = {"success": True, "action": "press", "keys": keys}
        elif action == "key_down":
            for vk in modifier_vks + normal_vks:
                _key_event(vk, 0)
            result = {"success": True, "action": "key_down", "keys": keys}
        elif action == "key_up":
            for vk in modifier_vks + normal_vks:
                _key_event(vk, win32con.KEYEVENTF_KEYUP)
            result = {"success": True, "action": "key_up", "keys": keys}
        else:
            result = {"success": False, "error": f"未知的 press action: {action}"}
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    if verify and result.get("success"):
        time.sleep(_VERIFY_WAIT)
        after = capture_signals(target_rect=target_rect)
        result["_verify"] = compare_signals(before, after)
    elif verify and not result.get("success"):
        result["_verify"] = _verify_failure(result.get("error", "未知错误"))

    return result


def select_text(cid: int, start: int, end: int,
                verify: str = "full", cache: Optional[ControlCache] = None) -> dict:
    """选中指定控件内第 start 到第 end 个字符。信号: foreground, visual。"""
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    if verify != "none":
        target_rect = _control_rect(control)
        before = capture_signals(target_rect=target_rect)

    # 尝试 TextPattern
    try:
        import uiautomation as uia
        tp = control.GetPattern(uia.PatternId.TextPattern)
        if tp:
            doc_range = tp.DocumentRange
            range_obj = doc_range.Clone()
            range_obj.MoveEndpointByUnit(
                uia.TextPatternRangeEndpoint.Start,
                uia.TextUnit.Character, start)
            range_obj.MoveEndpointByUnit(
                uia.TextPatternRangeEndpoint.End,
                uia.TextUnit.Character, end - start)
            range_obj.Select()
            selected_text = range_obj.GetText(-1) or ""
            result = {"success": True, "action": "select", "id": cid,
                      "start": start, "end": end, "selected": selected_text,
                      "method": "TextPattern"}
            if verify != "none":
                time.sleep(_VERIFY_WAIT)
                after = capture_signals(target_rect=target_rect)
                result["_verify"] = compare_signals(before, after)
            return result
    except Exception:
        pass

    # fallback: 鼠标拖拽选中文本
    try:
        bb = control.BoundingRectangle
        if bb:
            cx = int(bb.left + 5 + start * 8)
            cy = int((bb.top + bb.bottom) / 2)
            cx2 = int(bb.left + 5 + end * 8)
            _mouse_move(cx, cy)
            time.sleep(0.05)
            import win32api
            import win32con
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.1)
            _mouse_move(cx2, cy)
            time.sleep(0.1)
            win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            result = {"success": True, "action": "select", "id": cid,
                      "start": start, "end": end, "method": "mouse_drag"}
        else:
            result = {"success": False, "error": "无法获取控件矩形"}
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    if verify and result.get("success"):
        time.sleep(_VERIFY_WAIT)
        after = capture_signals(target_rect=target_rect)
        result["_verify"] = compare_signals(before, after)
    elif verify and not result.get("success"):
        result["_verify"] = _verify_failure(result.get("error", "未知错误"))

    return result


def scroll(cid: int, direction: str, amount: int = 3,
           verify: str = "full", cache: Optional[ControlCache] = None) -> dict:
    """对指定控件滚动。信号: foreground, visual, UIA滚动位置。"""
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    if verify != "none":
        target_rect = _control_rect(control)
        before = capture_signals(target_rect=target_rect)

    # 尝试 ScrollPattern
    try:
        import uiautomation as uia
        sp = control.GetPattern(uia.PatternId.ScrollPattern)
        if sp:
            h_amount = uia.ScrollAmount.NoAmount
            v_amount = uia.ScrollAmount.NoAmount
            if direction in ("down", "right"):
                scroll_unit = uia.ScrollAmount.SmallIncrement
            else:
                scroll_unit = uia.ScrollAmount.SmallDecrement
            if direction in ("up", "down"):
                v_amount = scroll_unit
            elif direction in ("left", "right"):
                h_amount = scroll_unit
            sp.Scroll(h_amount, v_amount)
            result = {"success": True, "action": "scroll", "id": cid,
                      "direction": direction, "amount": amount, "method": "ScrollPattern"}
            if verify != "none":
                time.sleep(_VERIFY_WAIT)
                after = capture_signals(target_rect=target_rect)
                result["_verify"] = compare_signals(before, after)
            return result
    except Exception:
        pass

    # fallback: 鼠标滚轮
    try:
        x, y = _clickable_point(control)
        wheel_amount = amount * 120 if direction in ("up", "down") else amount * 120
        if direction in ("up", "left"):
            wheel_amount = -wheel_amount
        _mouse_wheel(x, y, wheel_amount)
        result = {"success": True, "action": "scroll", "id": cid,
                  "direction": direction, "amount": amount, "method": "mouse_wheel"}
    except Exception as exc:
        result = {"success": False, "error": str(exc)}

    if verify and result.get("success"):
        time.sleep(_VERIFY_WAIT)
        after = capture_signals(target_rect=target_rect)
        result["_verify"] = compare_signals(before, after)
    elif verify and not result.get("success"):
        result["_verify"] = _verify_failure(result.get("error", "未知错误"))

    return result


def window_action(action: str, hwnd: Optional[int] = None,
                  x: Optional[int] = None, y: Optional[int] = None,
                  w: Optional[int] = None, h: Optional[int] = None,
                  verify: str = "full") -> dict:
    """
    窗口管理操作。信号: foreground, title, visual(窗口区域)。
    action: min/max/restore/close/focus/set_topmost/unset_topmost/move/resize
    """
    import win32gui
    import win32con

    if hwnd is None:
        hwnd = win32gui.GetForegroundWindow()
        if hwnd == 0:
            raise RuntimeError("无法获取当前活跃窗口")

    if verify != "none":
        target_rect = _window_rect_by_hwnd(hwnd)
        before = capture_signals(target_rect=target_rect)

    if action == "min":
        win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
    elif action == "max":
        win32gui.ShowWindow(hwnd, win32con.SW_MAXIMIZE)
    elif action == "restore":
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    elif action == "close":
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
    elif action == "focus":
        win32gui.SetForegroundWindow(hwnd)
    elif action == "set_topmost":
        win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    elif action == "unset_topmost":
        win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                              win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
    elif action == "move":
        if x is None or y is None:
            raise RuntimeError("move 需要 x, y 参数")
        win32gui.SetWindowPos(hwnd, 0, x, y, 0, 0,
                              win32con.SWP_NOSIZE | win32con.SWP_NOZORDER)
    elif action == "resize":
        if w is None or h is None:
            raise RuntimeError("resize 需要 w, h 参数")
        rect = win32gui.GetWindowRect(hwnd)
        win32gui.SetWindowPos(hwnd, 0, rect[0], rect[1], w, h,
                              win32con.SWP_NOZORDER)
    else:
        raise RuntimeError(f"未知的 window action: {action}")

    result = {"success": True, "action": action, "hwnd": hwnd}

    if verify != "none":
        time.sleep(_VERIFY_WAIT)
        # close/min 后窗口可能已销毁，用新的前台窗口 rect 采集 after
        if action in ("close", "min"):
            after = capture_signals(target_rect=None)  # 全屏截图
        else:
            # 其他操作窗口仍在，用原 rect 或更新后的 rect
            if action in ("move", "resize"):
                new_rect = _window_rect_by_hwnd(hwnd)
                after = capture_signals(target_rect=new_rect)
            else:
                after = capture_signals(target_rect=target_rect)
        result["_verify"] = compare_signals(before, after)

    return result


# ── 辅助 ────────────────────────────────────────────────────────

def _union_rects(rects: list) -> Optional[tuple]:
    """合并多个矩形为最小外接矩形。"""
    valid = [r for r in rects if r]
    if not valid:
        return None
    left = min(r[0] for r in valid)
    top = min(r[1] for r in valid)
    right = max(r[2] for r in valid)
    bottom = max(r[3] for r in valid)
    return (left, top, right, bottom)
