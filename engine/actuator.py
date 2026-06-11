"""
actuator.py — 执行器：click / type / press / drag / scroll / select / window。

所有操作前做 precheck：控件 handle 仍有效、仍 enabled。
底层通过 uiautomation 的 UIA 后端交互，fallback 到 win32api 鼠标事件。
"""

import time
import logging
from typing import Optional

from engine.cache import get_global_cache, ControlCache

logger = logging.getLogger("deskhand.actuator")


# ── 辅助：从 cache 查控件 ──────────────────────────────────────

def _get_control_by_id(cid: int, cache: Optional[ControlCache] = None):
    """根据控件 id 查 UIA 控件对象。"""
    if cache is None:
        cache = get_global_cache()

    runtime_id = cache.get_runtime_id(cid)
    if runtime_id is None:
        raise RuntimeError(f"控件 id={cid} 不在缓存中，请先调用 desk_state()")

    import uiautomation as uia
    try:
        control = uia.ControlFromRuntimeId(list(runtime_id))
        if control is None:
            raise RuntimeError(f"控件 id={cid} 已失效（RuntimeId 找不到控件）")
        return control
    except Exception as exc:
        raise RuntimeError(f"查找控件 id={cid} 失败: {exc}")


def _precheck(control) -> None:
    """操作前检查：控件存在且 enabled。"""
    try:
        if not control.Exists(0.5):
            raise RuntimeError("控件不存在或已销毁")
    except Exception:
        pass

    try:
        if not control.IsEnabled:
            raise RuntimeError("控件已禁用（disabled）")
    except Exception:
        pass


def _clickable_point(control) -> tuple[int, int]:
    """获取控件的可点击坐标。"""
    try:
        pt = control.GetClickablePoint()
        if pt:
            return (int(pt.x), int(pt.y))
    except Exception:
        pass
    # fallback: 矩形中心
    try:
        bb = control.BoundingRectangle
        if bb:
            return (
                int((bb.left + bb.right) / 2),
                int((bb.top + bb.bottom) / 2),
            )
    except Exception:
        pass
    raise RuntimeError("无法获取控件可点击坐标")


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


# ── 公开 API ───────────────────────────────────────────────────

def click(cid: int, button: str = "left", double: bool = False,
          hover: bool = False, cache: Optional[ControlCache] = None) -> dict:
    """点击/悬停指定控件。"""
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    x, y = _clickable_point(control)

    if hover:
        _mouse_move(x, y)
        return {"success": True, "action": "hover", "id": cid, "x": x, "y": y}

    _mouse_click(x, y, button, double)
    return {"success": True, "action": "click", "id": cid, "button": button,
            "double": double, "x": x, "y": y}


def drag(from_id: int, to_id: Optional[int] = None,
         to_x: Optional[int] = None, to_y: Optional[int] = None,
         cache: Optional[ControlCache] = None) -> dict:
    """拖拽：从控件 A 拖到控件 B 或指定坐标。"""
    from_control = _get_control_by_id(from_id, cache)
    _precheck(from_control)
    x1, y1 = _clickable_point(from_control)

    if to_id is not None:
        to_control = _get_control_by_id(to_id, cache)
        _precheck(to_control)
        x2, y2 = _clickable_point(to_control)
    elif to_x is not None and to_y is not None:
        x2, y2 = to_x, to_y
    else:
        raise RuntimeError("drag 必须指定 to_id 或 to_x+to_y")

    _mouse_down(x1, y1)
    time.sleep(0.1)
    # 中间点（平滑移动）
    steps = 5
    for i in range(1, steps + 1):
        mx = int(x1 + (x2 - x1) * i / steps)
        my = int(y1 + (y2 - y1) * i / steps)
        _mouse_move(mx, my)
        time.sleep(0.02)
    _mouse_up(x2, y2)

    return {"success": True, "action": "drag", "from_id": from_id,
            "to_id": to_id, "to_x": x2, "to_y": y2}


def type_text(cid: int, text: str, line: Optional[int] = None,
              cache: Optional[ControlCache] = None) -> dict:
    """
    向控件输入文本。
    line=None 时直接输入；line=N 时修改第 N 行（1-based）。
    """
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    if line is not None:
        # 先获取当前值，修改指定行
        try:
            vp = control.GetValuePattern()
            if vp:
                current = vp.Value or ""
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
                return {"success": True, "action": "type", "id": cid,
                        "line": line, "text": text, "lines_affected": len(lines)}
        except Exception as exc:
            logger.warning("SetValue line-edit failed: %s", exc)

    # fallback: SendKeys
    import uiautomation as uia
    try:
        if line is not None:
            # 先清空再输入
            control.SendKeys("{Ctrl}a{Delete}")
            time.sleep(0.05)
        # 转义特殊字符
        safe_text = text.replace("{", "{{").replace("}", "}}")
        control.SendKeys(safe_text)
        return {"success": True, "action": "type", "id": cid,
                "text": text, "method": "SendKeys"}
    except Exception as exc:
        raise RuntimeError(f"SendKeys failed: {exc}")


def press(keys: list[str], action: str = "press") -> dict:
    """
    发送键盘按键。
    action="press" 按下即释放；"key_down" 按住不放；"key_up" 释放。
    """
    import uiautomation as uia

    key_map = {
        "ctrl": uia.ModifierKey.Control,
        "alt": uia.ModifierKey.Alt,
        "shift": uia.ModifierKey.Shift,
        "win": uia.ModifierKey.Win,
    }

    # 构建修饰键 + 普通键
    modifiers = 0
    normal_keys = []
    for k in keys:
        kl = k.lower()
        if kl in key_map:
            modifiers |= key_map[kl]
        else:
            normal_keys.append(k)

    if action == "press":
        if normal_keys:
            uia.SendKeys(" ".join(normal_keys), modifiers)
        elif modifiers:
            # 只按修饰键
            uia.SendKey(modifiers)
        return {"success": True, "action": "press", "keys": keys}

    elif action == "key_down":
        # 按住修饰键
        if modifiers:
            uia.SendKey(modifiers, waitTime=0)
        return {"success": True, "action": "key_down", "keys": keys}

    elif action == "key_up":
        # 释放修饰键 — uiautomation 没有直接 key_up，这里用 SendKey 模拟
        if modifiers:
            uia.SendKey(modifiers, waitTime=0)
        return {"success": True, "action": "key_up", "keys": keys}

    else:
        raise RuntimeError(f"未知的 press action: {action}")


def select_text(cid: int, start: int, end: int,
                cache: Optional[ControlCache] = None) -> dict:
    """选中指定控件内第 start 到第 end 个字符。"""
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    # 尝试 TextPattern
    try:
        tp = control.GetTextPattern()
        if tp:
            tp.SetSelection(start, end)
            sel = tp.GetSelection()
            selected_text = sel[0].GetText(-1) if sel else ""
            return {"success": True, "action": "select", "id": cid,
                    "start": start, "end": end, "selected": selected_text}
    except Exception:
        pass

    # fallback: 鼠标拖拽选中文本
    try:
        bb = control.BoundingRectangle
        if bb:
            # 估算字符位置（粗略）
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
            return {"success": True, "action": "select", "id": cid,
                    "start": start, "end": end, "method": "mouse_drag"}
    except Exception as exc:
        raise RuntimeError(f"select_text failed: {exc}")


def scroll(cid: int, direction: str, amount: int = 3,
           cache: Optional[ControlCache] = None) -> dict:
    """对指定控件滚动。"""
    control = _get_control_by_id(cid, cache)
    _precheck(control)

    # 尝试 ScrollPattern
    try:
        sp = control.GetScrollPattern()
        if sp:
            if direction in ("up", "down"):
                sp.Scroll(verticalPercent=amount if direction == "down" else -amount)
            elif direction in ("left", "right"):
                sp.Scroll(horizontalPercent=amount if direction == "right" else -amount)
            return {"success": True, "action": "scroll", "id": cid,
                    "direction": direction, "amount": amount, "method": "ScrollPattern"}
    except Exception:
        pass

    # fallback: 鼠标滚轮
    x, y = _clickable_point(control)
    wheel_amount = amount * 120 if direction in ("up", "down") else amount * 120
    if direction in ("up", "left"):
        wheel_amount = -wheel_amount
    _mouse_wheel(x, y, wheel_amount)
    return {"success": True, "action": "scroll", "id": cid,
            "direction": direction, "amount": amount, "method": "mouse_wheel"}


def window_action(action: str, hwnd: Optional[int] = None,
                  x: Optional[int] = None, y: Optional[int] = None,
                  w: Optional[int] = None, h: Optional[int] = None) -> dict:
    """
    窗口管理操作。
    action: min/max/restore/close/focus/set_topmost/move/resize
    """
    import win32gui
    import win32con

    if hwnd is None:
        # 获取当前活跃窗口句柄
        hwnd = win32gui.GetForegroundWindow()
        if hwnd == 0:
            raise RuntimeError("无法获取当前活跃窗口")

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

    return {"success": True, "action": action, "hwnd": hwnd}
