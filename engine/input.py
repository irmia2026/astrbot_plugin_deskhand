"""
input.py — win32 键鼠执行器（无任何 UIA/COM 依赖）。

相对 v1 的修正：
- type_text 使用 KEYEVENTF_UNICODE，任意字符（含中文/emoji/符号）不依赖键盘布局；
- press 正确处理 VkKeyScan 的 shift 状态位（v1 会把 "!" 按成 "1"）；
- scroll 水平方向使用 MOUSEEVENTF_HWHEEL（v1 用垂直轮滚水平）。

所有函数均为同步阻塞，约定在 desktop.run() 的专用线程中调用。
"""

from __future__ import annotations

import time
from typing import Optional

_BTN_DOWN_UP = {}


def _win32():
    import win32api
    import win32con

    return win32api, win32con


# ── 鼠标 ────────────────────────────────────────────────────────

def move_to(x: int, y: int, steps: int = 0) -> None:
    """移动光标。steps>0 时做短暂分段移动（触发 hover 态、避免瞬移被某些 UI 忽略）。"""
    win32api, _ = _win32()
    if steps <= 0:
        win32api.SetCursorPos((int(x), int(y)))
        return
    x0, y0 = win32api.GetCursorPos()
    for i in range(1, steps + 1):
        nx = int(x0 + (x - x0) * i / steps)
        ny = int(y0 + (y - y0) * i / steps)
        win32api.SetCursorPos((nx, ny))
        time.sleep(0.015)


def click(x: int, y: int, button: str = "left", double: bool = False) -> dict:
    win32api, win32con = _win32()
    btn_map = {
        "left": (win32con.MOUSEEVENTF_LEFTDOWN, win32con.MOUSEEVENTF_LEFTUP),
        "right": (win32con.MOUSEEVENTF_RIGHTDOWN, win32con.MOUSEEVENTF_RIGHTUP),
        "middle": (win32con.MOUSEEVENTF_MIDDLEDOWN, win32con.MOUSEEVENTF_MIDDLEUP),
    }
    down, up = btn_map.get(button, btn_map["left"])
    move_to(x, y, steps=6)
    time.sleep(0.05)
    times = 2 if double else 1
    for _ in range(times):
        win32api.mouse_event(down, 0, 0, 0, 0)
        time.sleep(0.04)
        win32api.mouse_event(up, 0, 0, 0, 0)
        if double:
            time.sleep(0.05)
    return {"x": x, "y": y, "button": button, "double": double}


def hover(x: int, y: int) -> None:
    move_to(x, y, steps=6)
    time.sleep(0.1)


def drag(x1: int, y1: int, x2: int, y2: int, steps: int = 12) -> dict:
    win32api, win32con = _win32()
    move_to(x1, y1, steps=5)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.1)
    for i in range(1, steps + 1):
        nx = int(x1 + (x2 - x1) * i / steps)
        ny = int(y1 + (y2 - y1) * i / steps)
        win32api.SetCursorPos((nx, ny))
        time.sleep(0.02)
    time.sleep(0.05)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    return {"from": [x1, y1], "to": [x2, y2]}


def scroll(x: int, y: int, direction: str, amount: int = 3) -> dict:
    """滚轮。up/down 用垂直轮，left/right 用水平轮（MOUSEEVENTF_HWHEEL）。

    Win32 语义：WHEEL 正值 = 向上滚，HWHEEL 正值 = 向右滚。
    """
    win32api, win32con = _win32()
    move_to(x, y, steps=4)
    time.sleep(0.05)
    delta = int(amount) * 120
    if direction in ("down", "left"):
        delta = -delta
    if direction in ("left", "right"):
        win32api.mouse_event(win32con.MOUSEEVENTF_HWHEEL, 0, 0, delta, 0)
    else:
        win32api.mouse_event(win32con.MOUSEEVENTF_WHEEL, 0, 0, delta, 0)
    return {"direction": direction, "amount": amount}


# ── 键盘 ────────────────────────────────────────────────────────

# ── 文本输入（UNICODE / 剪贴板双通道）─────────────────────────────
#
# 教训：keybd_event + KEYEVENTF_UNICODE 在实测中一个字都打不进去（标准 EDIT 控件
# 回读为空）。唯一可靠的注入 API 是 SendInput。
# 中文等非 ASCII 文本默认走剪贴板粘贴（100% 可靠，自动保存/恢复剪贴板）。

import ctypes
from ctypes import wintypes

_INPUT_KEYBOARD = 1
_KEYEVENTF_UNICODE = 0x4
_KEYEVENTF_KEYUP = 0x2


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG), ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUT_UNION)]


def _send_unicode_char(ch: str) -> None:
    """用 SendInput 发送一个 Unicode 字符（>U+FFFF 自动拆代理对）。

    检查 SendInput 返回值：被 UIPI（目标是提权进程）拦截时会静默丢字，
    必须抛异常让上层报告失败。
    """
    code = ord(ch)
    if code > 0xFFFF:
        code -= 0x10000
        units = [0xD800 + (code >> 10), 0xDC00 + (code & 0x3FF)]
    else:
        units = [code]
    events = []
    for u in units:
        for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
            ev = _INPUT()
            ev.type = _INPUT_KEYBOARD
            ev.union.ki = _KEYBDINPUT(0, u, flags, 0, None)
            events.append(ev)
    arr = (_INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))
    if sent != len(events):
        err = ctypes.get_last_error()
        raise RuntimeError(
            f"SendInput 被拦截（{sent}/{len(events)}，GetLastError={err}）"
            "——目标窗口可能是提权进程（UIPI 隔离）"
        )


def _type_unicode(text: str, interval: float) -> None:
    win32api, win32con = _win32()
    for ch in text:
        if ch == "\n":
            win32api.keybd_event(win32con.VK_RETURN, 0, 0, 0)
            win32api.keybd_event(win32con.VK_RETURN, 0, win32con.KEYEVENTF_KEYUP, 0)
        elif ch == "\t":
            win32api.keybd_event(win32con.VK_TAB, 0, 0, 0)
            win32api.keybd_event(win32con.VK_TAB, 0, win32con.KEYEVENTF_KEYUP, 0)
        else:
            _send_unicode_char(ch)
        time.sleep(interval)


def _clipboard_paste(text: str) -> None:
    """剪贴板通道：保存原剪贴板文本 → 写入目标文本 → Ctrl+V → 恢复原内容。"""
    import win32clipboard

    win32api, win32con = _win32()
    old_text = None
    win32clipboard.OpenClipboard()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            try:
                old_text = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            except Exception:
                old_text = None
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()

    time.sleep(0.05)
    win32api.keybd_event(win32con.VK_CONTROL, 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(ord("V"), 0, 0, 0)
    time.sleep(0.02)
    win32api.keybd_event(ord("V"), 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(win32con.VK_CONTROL, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(0.3)  # 等粘贴完成再恢复剪贴板

    if old_text is not None:
        # 恢复路径必须 finally 关剪贴板：执行器线程常驻，一旦漏关，
        # 全局剪贴板被本进程长期锁定（其他应用也无法复制粘贴）
        try:
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, old_text)
            finally:
                win32clipboard.CloseClipboard()
        except Exception as e:
            import logging

            logging.getLogger("deskhand.input").warning("剪贴板恢复失败: %s", e)


def type_text(text: str, interval: float = 0.02, method: str = "auto") -> dict:
    """输入任意文本（支持中文/emoji）。

    method:
    - auto：含非 ASCII 字符走剪贴板粘贴（最可靠），纯 ASCII 走 SendInput 逐键；
    - unicode：强制 SendInput UNICODE 逐键注入；
    - clipboard：强制剪贴板粘贴。
    """
    if method == "clipboard":
        _clipboard_paste(text)
        used = "clipboard"
    elif method == "unicode":
        _type_unicode(text, interval)
        used = "unicode"
    else:  # auto
        if all(ord(c) < 128 for c in text):
            _type_unicode(text, interval)
            used = "unicode"
        else:
            _clipboard_paste(text)
            used = "clipboard"
    return {"len": len(text), "method": used}


_NAMED_KEYS = {
    "enter": "VK_RETURN", "return": "VK_RETURN", "tab": "VK_TAB",
    "esc": "VK_ESCAPE", "escape": "VK_ESCAPE", "space": "VK_SPACE",
    "backspace": "VK_BACK", "delete": "VK_DELETE", "insert": "VK_INSERT",
    "up": "VK_UP", "down": "VK_DOWN", "left": "VK_LEFT", "right": "VK_RIGHT",
    "home": "VK_HOME", "end": "VK_END",
    "pageup": "VK_PRIOR", "pagedown": "VK_NEXT",
    "f1": "VK_F1", "f2": "VK_F2", "f3": "VK_F3", "f4": "VK_F4",
    "f5": "VK_F5", "f6": "VK_F6", "f7": "VK_F7", "f8": "VK_F8",
    "f9": "VK_F9", "f10": "VK_F10", "f11": "VK_F11", "f12": "VK_F12",
}

_MODIFIERS = {
    "ctrl": "VK_CONTROL", "control": "VK_CONTROL",
    "alt": "VK_MENU", "shift": "VK_SHIFT", "win": "VK_LWIN",
}


def _resolve_key(key: str) -> tuple[int, list[int]]:
    """把按键名解析为 (vk, 额外需要的修饰 vk 列表)。正确处理 shift 状态位。"""
    win32api, win32con = _win32()
    kl = key.strip().lower()
    if kl in _MODIFIERS:
        vk = getattr(win32con, _MODIFIERS[kl])
        return vk, [vk]
    if kl in _NAMED_KEYS:
        return getattr(win32con, _NAMED_KEYS[kl]), []
    if len(key) == 1:
        v = win32api.VkKeyScan(key)
        if v != -1:
            vk = v & 0xFF
            state = (v >> 8) & 0xFF
            extra = []
            if state & 1:
                extra.append(win32con.VK_SHIFT)
            if state & 2:
                extra.append(win32con.VK_CONTROL)
            if state & 4:
                extra.append(win32con.VK_MENU)
            return vk, extra
        # VkKeyScan 无法映射时不能静默发废键，与多字符分支一致地报错
        raise ValueError(f"未知按键: {key}")
    raise ValueError(f"未知按键: {key}")


# 扩展键集合：这些键的扫描码需要 KEYEVENTF_EXTENDEDKEY
# （方向键/Home/End/PgUp/PgDn/Ins/Del/Win/右Ctrl/右Alt/小键盘除号）
_EXTENDED_VKS = {
    0x21, 0x22, 0x23, 0x24,  # PRIOR NEXT END HOME
    0x25, 0x26, 0x27, 0x28,  # LEFT UP RIGHT DOWN
    0x2D, 0x2E,              # INSERT DELETE
    0x5B, 0x5C,              # LWIN RWIN
    0x6F,                    # DIVIDE
    0xA3, 0xA5,              # RCONTROL RMENU
}


def _key_event(vk: int, keyup: bool = False) -> None:
    """以扫描码方式发送按键（KEYEVENTF_SCANCODE）。

    实测依据：SDL2/pygame/DirectInput 类游戏只认扫描码——
    keybd_event(vk, 0, ...) 画面差异 0.16%（没收到），
    keybd_event(0, scancode, SCANCODE) 差异 28.93%（收到了）。
    """
    win32api, win32con = _win32()
    sc = win32api.MapVirtualKey(vk, 0)  # MAPVK_VK_TO_VSC
    flags = win32con.KEYEVENTF_SCANCODE
    if vk in _EXTENDED_VKS:
        flags |= win32con.KEYEVENTF_EXTENDEDKEY
    if keyup:
        flags |= win32con.KEYEVENTF_KEYUP
    win32api.keybd_event(0, sc, flags, 0)


def press(keys: list[str], interval: float = 0.03) -> dict:
    """组合键：修饰键按住 → 普通键依次点按（含其自身需要的 shift 等）→ 修饰键释放。

    全部走扫描码通道，对游戏/模拟器/远程桌面同样有效。
    """
    if not keys:
        raise ValueError("keys 不能为空")

    mods: list[int] = []
    normals: list[tuple[int, list[int]]] = []
    for k in keys:
        kl = k.strip().lower()
        vk, extra = _resolve_key(k)
        if kl in _MODIFIERS:
            mods.append(vk)
        else:
            normals.append((vk, extra))

    try:
        for vk in mods:
            _key_event(vk)
            time.sleep(interval)
        for vk, extra in normals:
            for evk in extra:
                _key_event(evk)
                time.sleep(0.01)
            _key_event(vk)
            time.sleep(interval)
            _key_event(vk, keyup=True)
            for evk in reversed(extra):
                _key_event(evk, keyup=True)
        time.sleep(interval)
    finally:
        for vk in reversed(mods):
            _key_event(vk, keyup=True)
    return {"keys": keys}
