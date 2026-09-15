"""
wgc.py — Windows.Graphics.Capture 窗口截图（被遮挡窗口的真实画面）。

为什么需要它：
普通截屏（ImageGrab）抓的是**屏幕区域**——窗口被遮挡时抓到的是遮挡物。
UIA 后台动作（Invoke/SetValue）不改变前台焦点，窗口常常不在前台，
此时「动作前后像素对比」这条验证通道就断了。WGC 直接抓窗口自身的合成画面，
与遮挡无关，正好补上这一环。

实现与踩过的路（都别再走一遍）：
- PyWinRT（winsdk）本身做不了：`Direct3D11CaptureFramePool.create_free_threaded`
  只接受真正的 `_winrt.Object`，而 `ID3D11Device→IDirect3DDevice` 必须由
  `CreateDirect3D11DeviceFromDXGIDevice` 从裸 COM 指针造——纯 Python 过不去；
  写 C 扩展能通，但会把插件绑死在特定 CPython ABI 上（分发不可接受）。
- 最终用 **`windows-capture`**（Rust 封装的 WGC，cp38–cp312 预编译 wheel，
  原生支持 `window_hwnd=`）。
- **必须长驻 session**：该库每次新建 `WindowsCapture` 会线性泄漏
  **+11 句柄 / +8MB**（实测 100 次：413→1502 句柄、80.6→865.7MB）；
  泄漏在 session 创建/销毁，不在取帧（长驻 session 下 150 次捕获 Δ句柄=0）。
  所以这里按 hwnd 缓存长驻 session。
- **线程纪律**：desktop 执行器线程是 STA（UIA 要 STA），而本库内部按 MTA 初始化
  WinRT，直接在该线程建 session 会报 “Failed to initialize WinRT”。
  故所有 session 操作都在本模块自己的单线程执行器上跑。
- 帧尺寸是 **DWM extended frame bounds**（不是客户区、也不等于 GetWindowRect），
  所以只用于像素级验证，不用于坐标换算。
- 像素是 BGRA8、紧凑、已是屏幕方向（无需上下翻转）。

可选依赖：未安装 windows-capture 时 available()=False，上层静默回落
（只做状态回读/控件树验证）。注意它会带入 opencv-python（与既有的
opencv-python-headless 同源，版本不一致时建议对齐）。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

logger = logging.getLogger("deskhand.wgc")

_SPEC = (
    importlib.util.find_spec("windows_capture") if sys.platform == "win32" else None
)

# 捕获专用单线程执行器：MTA 由 windows-capture 自己初始化，此线程绝不碰 UIA/STA。
_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="deskhand-wgc")

_sessions: dict = {}          # hwnd -> _Session（长驻，避免库的 session 泄漏）
_sessions_lock = threading.RLock()
_grab_locks: dict = {}        # hwnd -> Lock（同一窗口的取帧串行化）
_last_returned_seq: dict = {}  # hwnd -> 上次**返回给调用方**的帧序号
# 「是否有新帧」必须相对上次返回的序号判断，不能在同一次调用内部比：
# 调用开始时快照已是新帧的情况很常见（取图前的等待期间就来了），
# 内部比较会误报 newer=False（实测踩过：seq 2→5 却报 newer=False）。
_last_reason: str = ""

# 陈旧帧警告（实测发现，必须让上层知道）：
# 窗口被**完全遮挡**时 DWM 不再为它合成新帧 → WGC 只会送到最后一张旧帧。
# 实测：可见时后台改变界面 seq 1→5（有新帧）；遮挡后同样操作 seq 7→7（无新帧）；
# 取消遮挡后又 7→9。因此「没拿到新帧」绝不能被当成「画面没变化」。
# 语义：capture_window(..., wait_newer=True) 会在拿不到新帧时置位，
# 上层据此判定“本次没有像素证据”，回到状态回读/控件树证据。


def available() -> bool:
    """windows-capture 是否可导入（只探测，不真正导入）。"""
    return _SPEC is not None


def last_reason() -> str:
    """最近一次捕获失败的原因（给上层做诊断/日志，不参与逻辑判断）。"""
    return _last_reason


# ── 前置检查（把「等 3 秒超时」变成「立刻给结论」）────────────────

def _precheck(hwnd: int) -> Optional[str]:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    if not hwnd or not user32.IsWindow(wintypes.HWND(int(hwnd))):
        return "invalid hwnd (window destroyed?)"
    if user32.IsIconic(wintypes.HWND(int(hwnd))):
        # 最小化窗口不再渲染，WGC 永远等不到帧（实测绕过检查会是 1s 超时）
        return "window is minimized (no rendered content)"
    return None


# ── 长驻 session ────────────────────────────────────────────────

class _Session:
    """一个 hwnd 一个长驻 WGC 会话，最新帧存在槽里。

    `latest()` 不阻塞直接取槽里的帧；`wait_newer()` 等一帧更新的。
    长驻的理由见模块 docstring（windows-capture 的 session 构造会泄漏）。
    """

    def __init__(self, hwnd: int) -> None:
        from windows_capture import WindowsCapture

        self.hwnd = int(hwnd)
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.seq = 0
        self.arr = None            # 最新一帧（已拷贝的 BGRA numpy 数组）
        self.frame_at = 0.0
        self.closed = False
        self.error: Optional[str] = None
        self.capture = WindowsCapture(
            window_hwnd=self.hwnd, cursor_capture=False,
            draw_border=False, secondary_window=True,
        )
        self.capture.frame_handler = self._on_frame
        self.capture.closed_handler = self._on_closed
        self.control = self.capture.start_free_threaded()

    # 回调跑在库自己的捕获线程上
    def _on_frame(self, frame, control) -> None:  # noqa: ANN001
        try:
            # frame_buffer 是「零拷贝视图」，回调返回后底层映射即失效——必须立刻拷贝
            arr = frame.frame_buffer.copy()
        except Exception as e:
            self.error = f"frame copy failed: {e}"
            return
        with self.lock:
            self.arr = arr
            self.seq += 1
            self.frame_at = time.perf_counter()
        self.event.set()

    def _on_closed(self) -> None:
        self.closed = True
        self.event.set()

    def snapshot(self):
        with self.lock:
            return self.seq, self.arr, self.frame_at

    def wait_newer(self, last_seq: int, timeout_s: float):
        deadline = time.perf_counter() + timeout_s
        while True:
            seq, arr, at = self.snapshot()
            if (seq > last_seq and arr is not None) or self.closed:
                return seq, arr, at
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                return seq, arr, at
            self.event.wait(min(remaining, 0.05))
            self.event.clear()

    def stop(self) -> None:
        try:
            if self.control is not None:
                self.control.stop()
                self.control.wait()  # 不 wait 的话捕获线程会活得比 session 久
        except Exception:
            pass
        self.capture = None
        self.control = None


def _grab_lock(hwnd: int) -> threading.Lock:
    with _sessions_lock:
        lk = _grab_locks.get(hwnd)
        if lk is None:
            lk = threading.Lock()
            _grab_locks[hwnd] = lk
        return lk


def _get_session(hwnd: int) -> tuple:
    with _sessions_lock:
        sess = _sessions.get(hwnd)
        if sess is not None and not sess.closed:
            return sess, True
        if sess is not None:
            sess.stop()
            _sessions.pop(hwnd, None)
        sess = _Session(hwnd)
        _sessions[hwnd] = sess
        return sess, False


def release(hwnd: int) -> bool:
    """释放某个窗口的会话。"""
    with _sessions_lock:
        sess = _sessions.pop(hwnd, None)
        _grab_locks.pop(hwnd, None)
        _last_returned_seq.pop(hwnd, None)
    if sess is None:
        return False
    sess.stop()
    return True


def close_all() -> int:
    """释放全部会话（插件 terminate 时调用）。返回释放数量。"""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
        _grab_locks.clear()
        _last_returned_seq.clear()
    for s in sessions:
        try:
            s.stop()
        except Exception:
            pass
    return len(sessions)


def session_count() -> int:
    with _sessions_lock:
        return len(_sessions)


# ── 捕获 ────────────────────────────────────────────────────────

def _capture_sync(hwnd: int, wait_newer: bool, timeout: float,
                  fresh_timeout: float):
    """在 WGC 专用线程里执行：返回 (PIL.Image | None, 原因, 诊断 dict)。"""
    global _last_reason
    import numpy as np
    from PIL import Image

    diag = {"newer": None, "seq": None, "frame_age_ms": None}
    reason = _precheck(hwnd)
    if reason:
        _last_reason = reason
        return None, reason, diag

    try:
        sess, reused = _get_session(hwnd)
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        _last_reason = reason
        return None, reason, diag

    diag["reused"] = reused
    with _grab_lock(hwnd):
        seq, arr, at = sess.snapshot()
        if arr is None:
            # 新会话：等首帧（实测首次约 100–200ms）
            seq, arr, at = sess.wait_newer(0, timeout)
        else:
            prev = _last_returned_seq.get(hwnd)
            if wait_newer and (prev is None or seq <= prev):
                # 还停在上次返回的那一帧：等一等有没有更新的；
                # **等不到就如实标记 newer=False**——完全遮挡的窗口 DWM 不合成新帧，
                # 此时绝不能把旧帧当作“画面没变”。
                new_seq, new_arr, new_at = sess.wait_newer(seq, fresh_timeout)
                if new_arr is not None:
                    seq, arr, at = new_seq, new_arr, new_at
            diag["newer"] = None if prev is None else (seq > prev)
        _last_returned_seq[hwnd] = seq if arr is not None else _last_returned_seq.get(hwnd)
    diag["seq"] = seq
    if at:
        diag["frame_age_ms"] = round((time.perf_counter() - at) * 1000, 1)

    if arr is None:
        if sess.closed:
            release(hwnd)
            reason = "window closed before a frame arrived"
        else:
            reason = (f"no frame within {timeout:.1f}s "
                      "(minimized, never-rendering, or protected content)")
        _last_reason = reason
        return None, reason, diag
    try:
        # BGRA8、紧凑、(h, w, 4)、已是屏幕方向
        img = Image.fromarray(np.asarray(arr)[:, :, [2, 1, 0]], "RGB")
    except Exception as e:
        reason = f"BGRA->PIL failed: {e}"
        _last_reason = reason
        return None, reason, diag
    _last_reason = ""
    return img, "", diag


def capture_window_ex(hwnd: int, wait_newer: bool = False, timeout: float = 3.0,
                      fresh_timeout: float = 0.5):
    """带诊断的捕获：返回 (PIL.Image | None, diag)。

    diag["newer"]：wait_newer=True 时，是否真的拿到了**比上次更新**的一帧。
    False 表示窗口在此期间没有重新合成（**完全被遮挡时 DWM 不合成**，实测 seq 不增长）——
    这种情况上层必须判定为「没有像素证据」，绝不能当成「画面没变化」。
    """
    if not available():
        return None, {"reason": "windows-capture 未安装"}
    try:
        fut = _EXEC.submit(_capture_sync, hwnd, wait_newer, timeout, fresh_timeout)
        img, reason, diag = fut.result(timeout=timeout + fresh_timeout + 1.0)
        diag["reason"] = reason
        return img, diag
    except Exception as e:
        global _last_reason
        _last_reason = f"{type(e).__name__}: {e}"
        logger.warning("WGC 捕获异常（hwnd=%s）: %s", hwnd, e)
        return None, {"reason": _last_reason, "newer": None}


def capture_window(hwnd: int, wait_newer: bool = False, timeout: float = 3.0,
                   fresh_timeout: float = 0.5):
    """抓取指定窗口的画面（即使被遮挡），返回 PIL.Image；失败返回 None。

    wait_newer=True 时最多等 fresh_timeout 秒去拿一帧更新的画面
    （动作后取图用这个）。**注意**：窗口被完全遮挡时 DWM 不合成新帧，
    此时返回的仍是旧帧——需要区分「旧帧」与「画面没变」的调用方请用
    `capture_window_ex()` 看 diag["newer"]。

    同步阻塞：内部转交 WGC 专用线程执行（desktop 线程是 STA，本库要 MTA）。
    返回的是窗口自身画面（DWM extended frame bounds），尺寸不等于 GetWindowRect，
    只用于像素级验证，不用于坐标换算。失败原因见 last_reason()。
    """
    img, _diag = capture_window_ex(hwnd, wait_newer, timeout, fresh_timeout)
    return img


def shutdown() -> None:
    """兼容别名：释放会话并关闭执行器。"""
    close_all()
    try:
        _EXEC.shutdown(wait=False)
    except Exception:
        pass
