"""
desktop.py — 桌面基础层：单线程执行器、DPI 感知、窗口枚举、截图。

设计要点：
- 所有桌面操作（截图 / 鼠标 / 键盘 / OCR）都在同一个专用线程里串行执行：
  鼠标是全局共享资源，串行天然避免竞态；也不依赖任何 COM/UIA（v2 已全面转向视觉方案）。
- SetProcessDPIAware 保证 GetWindowRect 的坐标与 ImageGrab 截图像素一致。
"""

from __future__ import annotations

import asyncio
import ctypes
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional


def _init_worker() -> None:
    """执行器线程初始化：声明 DPI 感知（进程级，幂等）。"""
    if sys.platform == "win32":
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_EXEC = ThreadPoolExecutor(
    max_workers=1, thread_name_prefix="deskhand-desktop", initializer=_init_worker
)


async def run(fn, *args, **kwargs):
    """在桌面专用线程中执行同步函数。"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXEC, lambda: fn(*args, **kwargs))


def shutdown() -> None:
    """关闭桌面执行器（插件 terminate 时调用）。热重载会重建模块与新执行器。"""
    _EXEC.shutdown(wait=False)


# ── 窗口枚举与匹配 ──────────────────────────────────────────────

_SYSTEM_WINDOW_TITLES = {
    "",
    "program manager",
    "default ime",
    "msctfime ui",
    "application frame host",
    "windows shell experience host",
    "search",
    "设置",
    "任务视图",
    "任务栏",
}

_WINDOW_ALIASES = {
    "vs code": "visual studio code",
    "vscode": "visual studio code",
    "qq": "腾讯qq",
    "wechat": "微信",
}


def _rect_on_screen(rect) -> bool:
    """矩形是否与虚拟屏有交集（过滤 -21333 这类完全在屏外的幽灵窗口）。"""
    left, top, right, bottom = screen_bounds()
    return not (rect[2] <= left or rect[0] >= right or rect[3] <= top or rect[1] >= bottom)


def enum_windows() -> list[dict]:
    """枚举可见顶层窗口：[{hwnd, title, rect, class_name, iconic}]，z-order 从顶到底。

    过滤：不可见、系统窗、极小窗、矩形完全在虚拟屏之外的幽灵窗口。
    最小化窗口保留（rect=None）——否则 min 之后 restore 会匹配不到原窗口。
    """
    import win32gui

    found: list[dict] = []

    def _cb(hwnd: int, _) -> None:
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = (win32gui.GetWindowText(hwnd) or "").strip()
            if not title or title.lower() in _SYSTEM_WINDOW_TITLES:
                return
            # 最小化窗口的 GetWindowRect 返回无效/极小矩形（如 158×26），
            # 不能被尺寸过滤器误杀——否则 min 之后 restore 会匹配不到原窗口
            if win32gui.IsIconic(hwnd):
                found.append(
                    {
                        "hwnd": hwnd,
                        "title": title,
                        "rect": None,
                        "class_name": win32gui.GetClassName(hwnd) or "",
                        "iconic": True,
                    }
                )
                return
            rect = win32gui.GetWindowRect(hwnd)
            if rect[2] - rect[0] < 80 or rect[3] - rect[1] < 60:
                return
            if not _rect_on_screen(rect):
                return  # 幽灵窗口（坐标完全在屏外，如 -21333,-21333）
            found.append(
                {
                    "hwnd": hwnd,
                    "title": title,
                    "rect": tuple(rect),
                    "class_name": win32gui.GetClassName(hwnd) or "",
                    "iconic": False,
                }
            )
        except Exception:
            return

    win32gui.EnumWindows(_cb, None)
    return found


# ── 窗口句柄记忆：min/max 过的窗口，restore/focus 时直接用句柄，不重新匹配标题 ──
#
# hwnd 会被 OS 复用：窗口销毁后同一值可能分给无关新窗口。
# 所以记忆同时存 class_name 快照，recall 时复核，防止对错误窗口执行 close/min。

_window_memory: dict[str, dict] = {}


def remember_window(keyword: str, hwnd: int, class_name: str = "") -> None:
    kw = (keyword or "").strip().lower()
    if kw:
        _window_memory[kw] = {"hwnd": hwnd, "class_name": class_name or ""}


def recall_window(keyword: str) -> Optional[int]:
    """按上次匹配时的关键词回忆 hwnd（校验句柄有效 + 类名一致，防 hwnd 复用）。"""
    import win32gui

    kw = (keyword or "").strip().lower()
    rec = _window_memory.get(kw)
    if not rec:
        return None
    hwnd = rec["hwnd"]
    try:
        if not win32gui.IsWindow(hwnd):
            raise ValueError("gone")
        if rec["class_name"] and win32gui.GetClassName(hwnd) != rec["class_name"]:
            raise ValueError("class changed")
        return hwnd
    except Exception:
        _window_memory.pop(kw, None)
        return None


def find_window(keyword: str) -> Optional[dict]:
    """按标题关键词匹配窗口：别名展开 → 精确匹配 → 包含匹配；同级优先非最小化窗口。

    最小化的幽灵窗口（如标题恰好叫「QQ」的最小化窗口）会在精确匹配档被跳过，
    避免抢走真实窗口的匹配——需要操作最小化窗口时用 window_action。
    """
    kw = (keyword or "").strip().lower()
    if not kw:
        return None
    kws = [kw]
    expanded = _WINDOW_ALIASES.get(kw)
    if expanded:
        kws.append(expanded)

    wins = enum_windows()

    def _pick(candidates: list) -> Optional[dict]:
        normal = [w for w in candidates if not w.get("iconic")]
        return (normal or candidates or [None])[0]

    exact = [w for w in wins if w["title"].lower().strip() in kws]
    hit = _pick(exact)
    if hit:
        return hit
    contains = [w for w in wins if any(k in w["title"].lower() for k in kws)]
    return _pick(contains)


def foreground_window() -> Optional[dict]:
    """当前前台窗口（含 hwnd/title/rect/class_name）。"""
    import win32gui

    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return None
        return {
            "hwnd": hwnd,
            "title": (win32gui.GetWindowText(hwnd) or "").strip(),
            "rect": tuple(win32gui.GetWindowRect(hwnd)),
            "class_name": win32gui.GetClassName(hwnd) or "",
        }
    except Exception:
        return None


def window_rect(hwnd: int) -> Optional[tuple]:
    import win32gui

    try:
        return tuple(win32gui.GetWindowRect(hwnd))
    except Exception:
        return None


def valid_rect(rect) -> bool:
    """矩形有效且不是最小化窗口（最小化时 rect 约为 -32000）。"""
    if not rect or len(rect) != 4:
        return False
    left, top, right, bottom = rect
    return right > left and bottom > top and left > -30000 and top > -30000


def app_key(win: dict) -> str:
    """窗口所属应用的稳定标识（记忆库键）：优先 exe 文件名，兜底 class_name。

    用 class_name 会在 Chrome_WidgetWin_1 系（浏览器/QQ/各种 Electron）之间撞车。
    """
    import os

    try:
        import win32api
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(win["hwnd"])
        h = win32api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        try:
            exe = win32process.GetModuleFileNameEx(h, 0)
        finally:
            win32api.CloseHandle(h)
        name = os.path.basename(exe).strip().lower()
        if name:
            return name
    except Exception:
        pass
    return (win.get("class_name") or "unknown").lower()


# ── 截图 ────────────────────────────────────────────────────────

def virtual_screen_origin() -> tuple[int, int]:
    """虚拟屏原点（多显示器时可能为负值）。全屏截图的 (0,0) 对应该原点。"""
    try:
        x = ctypes.windll.user32.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        y = ctypes.windll.user32.GetSystemMetrics(77)  # SM_YVIRTUALSCREEN
        return int(x), int(y)
    except Exception:
        return (0, 0)


def screen_bounds() -> tuple[int, int, int, int]:
    """虚拟屏边界 (left, top, right, bottom)，用于坐标钳制。"""
    try:
        x = ctypes.windll.user32.GetSystemMetrics(76)
        y = ctypes.windll.user32.GetSystemMetrics(77)
        w = ctypes.windll.user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        h = ctypes.windll.user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if w > 0 and h > 0:
            return (int(x), int(y), int(x + w), int(y + h))
    except Exception:
        pass
    return (0, 0, 1 << 15, 1 << 15)

def screenshot(bbox: Optional[tuple] = None):
    """截屏（全虚拟屏或指定区域），返回 PIL.Image。调用方负责 close。"""
    from PIL import ImageGrab

    return ImageGrab.grab(bbox=bbox, all_screens=True)


def screenshot_window(hwnd: int):
    """截取指定窗口区域；窗口无效时退化为全屏。"""
    rect = window_rect(hwnd)
    if valid_rect(rect):
        return screenshot(rect)
    return screenshot()


def cursor_pos() -> tuple[int, int]:
    import win32api

    return tuple(win32api.GetCursorPos())


def coord_space_info(size: tuple[int, int]) -> dict:
    """返回坐标空间标注：物理像素（截图/click 使用的坐标系）与系统逻辑像素。

    进程是 DPI 感知的，截图和 win32 坐标都是物理像素；逻辑尺寸 = 物理 / (DPI/96)。
    """
    _init_worker()  # 幂等：确保 DPI 感知已设置，GetDpiForSystem 才返回真实 DPI
    phys_w, phys_h = int(size[0]), int(size[1])
    info = {"coordinate_space": "physical_pixels", "physical_size": [phys_w, phys_h]}
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        scale = dpi / 96.0
        info["scale_factor"] = round(scale, 3)
        info["logical_size"] = [round(phys_w / scale), round(phys_h / scale)]
    except Exception:
        info["scale_factor"] = None
        info["logical_size"] = None
    return info


def sleep(seconds: float) -> None:
    time.sleep(seconds)
