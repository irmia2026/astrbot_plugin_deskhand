"""
uia.py — UIA L0 层：控件树感知 + 后台模式执行（Cua Driver 方案）。

设计要点（POC 已验证）：
- 仅窗口模式启用：look(window=X) 时 UIA 先行，全屏/scan_scene 不走 UIA；
- COM 纪律：desktop 执行器线程内 STA 初始化（desktop._init_worker，
  CoInitializeEx(None, 0x2)；0x0=MTA 会 RPC_E_CHANGED_MODE）；
- uiautomation 必须在使用线程内导入——模块级 COM 对象（IUIAutomation 全局
  客户端）在哪个线程创建就绑在哪个线程的套间，跨线程使用是 v1 的线程炸弹。
  因此本模块惰性导入（find_spec 探测 + desktop 线程内首次调用时 import）；
- Control 对象禁止跨调用持有：列表与动作各自独立遍历，动作时按
  「元素中心最近匹配」重新定位控件再调 pattern，天然免疫快照过期；
- 可选依赖：uiautomation 未安装时 available()=False，全链路静默跳过。
"""

from __future__ import annotations

import ctypes
import importlib.util
import logging
import sys
import time
from collections import Counter
from typing import Optional

logger = logging.getLogger("deskhand.uia")

_MAX_DEPTH = 20   # 遍历深度上限
_MAX_COUNT = 1500  # 单次遍历节点数上限
_TIME_BUDGET = 2.0  # 单次遍历时间预算（秒）——防慢 provider
#
# 为什么从「深度 6 / 200 节点」（POC 时的拍脑袋保险丝）改成上面这组：
# 真实实测（probe_uia_depth.py，同一台机器）：深度 6 会大量丢控件——
#   msedge 6→40 个、explorer 37→125 个、AstrBot 桌面端 0→60 个、msedgewebview2 1→6 个；
# 而节点量与耗时几乎不涨（深度 20 时最多 482 节点 / 0.49s）。
# 也就是说：真正卡住控件发现的是**深度**，不是数量；数量/时间上限只当安全保险丝。
# 慢 provider（实测 Bandizip/updater 单次 UIA 调用 ~300ms，深度 14 要 14s）靠时间预算兜，
# 并记住这个应用（_slow_cache），后续 5 分钟内自动换成低预算模式。
_MAX_DEPTH_SLOW = 6      # 慢 provider 模式下的深度
_TIME_BUDGET_SLOW = 0.8  # 慢 provider 模式下的时间预算
_SLOW_TTL = 300.0        # 慢 provider 记忆时长（秒）

# click 重新定位控件时的最大中心距（像素）
_MAX_REMATCH_DIST = 24

# 窗口 chrome（最小化/最大化/关闭 等）不上面片：
# - Win32/WinForms/WPF：这些按钮挂在 TitleBarControl 之下 → 整棵子树跳过；
# - UWP（真实环境实测）：没有 TitleBarControl，chrome 按钮直接挂在窗口下 →
#   再用「标题栏条带内 + 名称含 chrome 关键词」兜底过滤。
# 理由：window_action 已覆盖窗口管理，卡片里混入「关闭」既浪费槽位又有误点风险。
_CHROME_KEYWORDS = ("最小化", "最大化", "关闭", "还原", "minimize", "maximize",
                    "close", "restore")
_TITLE_STRIP_BASE_PX = 48  # 96 DPI 下的标题栏条带高度

# 慢空树缓存（真实环境实测引入的必要优化）：
# Nahimic / Windows 输入体验 / 部分 UWP 句柄上，UIA 查询要 1.2-1.4s 才返回空树，
# 而 look() 会先等 UIA——不缓存的话这些应用每次 look 白等一秒多（v2.6 本来只花 ~1s）。
# 只缓存「耗时 > 0.5s 的空结果」：快速返回空的应用（Electron/WebView2 常在首次
# 查询后才激活无障碍树）必须每次重探，缓存掉会永久错过它们的内容。
_EMPTY_TTL = 120.0
_SLOW_EMPTY_SEC = 0.5
_empty_cache: dict[tuple, float] = {}  # (hwnd, app_key) -> 缓存时间戳（monotonic）
_slow_cache: dict[tuple, float] = {}   # 同上：命中过时间预算的“慢 provider”应用


def _is_minimized(hwnd: int) -> bool:
    """最小化窗口的 UIA 树是空的（控件全部 offscreen）——这种情况不得进空树缓存。"""
    try:
        import win32gui

        return bool(win32gui.IsIconic(hwnd))
    except Exception:
        return False


def _cache_key(hwnd: int) -> tuple:
    """缓存键含 exe 名：hwnd 会被 OS 复用，同值不同应用不得互相污染。"""
    try:
        from . import desktop

        app = desktop.app_key({"hwnd": hwnd, "class_name": ""})
    except Exception:
        app = ""
    return (hwnd, app)

_SPEC = (
    importlib.util.find_spec("uiautomation") if sys.platform == "win32" else None
)
_uia = None  # 惰性导入，只在 desktop 线程内首次使用时创建


def available() -> bool:
    """uiautomation 是否可导入（不在当前线程真正 import，只探测）。"""
    return _SPEC is not None


def _u():
    """在使用线程（desktop 执行器线程）内惰性导入 uiautomation。"""
    global _uia
    if _uia is None:
        if _SPEC is None:
            raise RuntimeError("uiautomation 未安装（pip install uiautomation）")
        import uiautomation

        _uia = uiautomation
    return _uia


def _impl():
    """拿 uiautomation 的实现子模块。

    包的 `__init__` 用 `import *` 导入，下划线开头的私有名（`_AutomationClient`）
    不会出现在包命名空间里，必须从子模块取（踩过 AttributeError）。
    """
    _u()
    import uiautomation.uiautomation as impl

    return impl


def _iuia():
    """返回 (typelib 常量模块, 裸 IUIAutomation 接口)。仅限 desktop 线程使用。"""
    client = _impl()._AutomationClient.instance()
    return client.UIAutomationCore, client.IUIAutomation


# ── 子树缓存遍历（主力路径）────────────────────────────────────
#
# 逐节点遍历每个控件要 ~8 次跨进程 COM 调用（name/type/rect/offscreen + 5 个 pattern
# 探测 + children），慢 provider（实测 Bandizip 单次 ~300ms）上一棵 43 节点的树要 7-9s。
# CacheRequest + TreeScope=Subtree 则是：**一次**跨进程调用把整棵子树的属性拉回来，
# 之后 GetCachedChildren() 全在本地内存走树。实测加速 3～14×：
#   qq 0.84→0.06s、msedge 0.37→0.03s、DSH 1.82→0.25s、AstrBot 0.90→0.14s、
#   Bandizip 9.04→3.18s、updater 5.36→4.23s
_CACHE_PROPS = (
    "UIA_NamePropertyId", "UIA_ControlTypePropertyId", "UIA_BoundingRectanglePropertyId",
    "UIA_IsOffscreenPropertyId", "UIA_IsEnabledPropertyId", "UIA_AutomationIdPropertyId",
    "UIA_IsInvokePatternAvailablePropertyId", "UIA_IsTogglePatternAvailablePropertyId",
    "UIA_IsValuePatternAvailablePropertyId", "UIA_IsExpandCollapsePatternAvailablePropertyId",
    "UIA_IsSelectionItemPatternAvailablePropertyId",
)
_CACHE_PATTERN_FLAGS = (
    ("UIA_IsInvokePatternAvailablePropertyId", "invoke"),
    ("UIA_IsTogglePatternAvailablePropertyId", "toggle"),
    ("UIA_IsValuePatternAvailablePropertyId", "value"),
    ("UIA_IsExpandCollapsePatternAvailablePropertyId", "expandcollapse"),
    ("UIA_IsSelectionItemPatternAvailablePropertyId", "selectionitem"),
)
_cache_supported: Optional[bool] = None  # 一次失败就不再重试（免得每次 look 都白付代价）


def _cached_rect(raw) -> Optional[tuple[int, int, int, int]]:
    """把 UIA 原始 BoundingRectangle 转成 (left, top, right, bottom)。

    ⚠ 坑：`UIA_BoundingRectanglePropertyId` 是 `double[4]` =
    **(left, top, width, height)**，而 uiautomation 库封装后返回的是
    left/top/right/bottom。必须自己做 l+w / t+h 换算，否则所有坐标静默偏移
    （实测对照：缓存 (398,489,1085,949) ↔ 库 right=1483 bottom=1438）。
    """
    try:
        if raw is None or len(raw) < 4:
            return None
        l, t, w, h = (int(round(float(v))) for v in raw[:4])
        if w <= 0 or h <= 0:
            return None
        return (l, t, l + w, t + h)
    except Exception:
        return None


def _collect_controls_cached(hwnd: int, win_rect=None, max_depth: int = _MAX_DEPTH,
                             max_count: int = _MAX_COUNT,
                             time_budget: float = _TIME_BUDGET) -> dict:
    """子树缓存遍历（主力路径）。与逐节点遍历返回同样的结构。

    注意：`AutomationElementMode_None` 下元素没有完整引用（这正是它快的原因），
    所以 entries 里的 ctrl 为 None；动作路径改用 `_control_at_point` 拿真实元素。
    """
    core, iuia = _iuia()
    cr = iuia.CreateCacheRequest()
    cr.TreeScope = core.TreeScope_Subtree
    cr.AutomationElementMode = core.AutomationElementMode_None
    pids: dict = {}
    for nm in _CACHE_PROPS:
        pid = getattr(core, nm, None)
        if pid is None:
            continue
        pids[nm] = pid
        cr.AddProperty(pid)

    t0 = time.monotonic()
    root = iuia.ElementFromHandleBuildCache(hwnd, cr)
    fetch_sec = time.monotonic() - t0

    out: list[dict] = []
    chrome_skipped = 0
    truncated = False
    reason = ""
    title_bar_type = getattr(core, "UIA_TitleBarControlTypeId", _CTYPE_TITLE_BAR)
    strip_bottom = win_rect[1] + _title_strip_height(hwnd) if win_rect else None
    stack = [(root, 0)]
    seen = 0
    while stack:
        if seen >= max_count:
            truncated, reason = True, "max_count"
            break
        if time_budget and time.monotonic() - t0 > time_budget:
            truncated, reason = True, "time_budget"
            break
        el, depth = stack.pop()
        seen += 1
        try:
            ctype = el.GetCachedPropertyValue(pids["UIA_ControlTypePropertyId"])
        except Exception:
            ctype = None
        if ctype == title_bar_type:
            chrome_skipped += 1
            continue  # 标题栏子树整棵跳过（不下降）
        rect = None
        if ctype is not None:
            try:
                if not el.GetCachedPropertyValue(pids["UIA_IsOffscreenPropertyId"]):
                    rect = _cached_rect(
                        el.GetCachedPropertyValue(pids["UIA_BoundingRectanglePropertyId"])
                    )
            except Exception:
                rect = None
        if rect is not None:
            try:
                name = str(el.GetCachedPropertyValue(pids["UIA_NamePropertyId"]) or "").strip()
            except Exception:
                name = ""
            if strip_bottom is not None and _looks_like_chrome(name, rect, strip_bottom):
                chrome_skipped += 1
            else:
                pats = {}
                for nm, short in _CACHE_PATTERN_FLAGS:
                    try:
                        if el.GetCachedPropertyValue(pids[nm]):
                            pats[short] = True
                    except Exception:
                        pass
                # 与逐节点路径保持一致的入选规则：有 pattern，或属于本身可交互的语义类型。
                # 漏掉这条会让卡片被 Text/Pane/根窗口等无 pattern 元素塞满（实测踩过）。
                etype = _type_name_by_id(ctype)
                if pats or etype in _KEEP_TYPES:
                    out.append({
                        "ctrl": None, "el": el, "name": name,
                        "type": etype, "rect": rect, "pats": pats,
                    })
        if depth < max_depth:
            try:
                arr = el.GetCachedChildren()
                kids = [arr.GetElement(i) for i in range(arr.Length)]
            except Exception:
                kids = []
            for k in reversed(kids):
                stack.append((k, depth + 1))
    return {"controls": out, "chrome_skipped": chrome_skipped,
            "truncated": truncated, "truncated_reason": reason,
            "visited": seen, "elapsed": round(time.monotonic() - t0, 2),
            "fetch_sec": round(fetch_sec, 2), "cached": True}


def _control_at_point(x: int, y: int, expect_name: str = "", expect_rect=None,
                      want_action: bool = True):
    """取屏幕上某点处的真实元素并包装成可操作的 Control，返回 (ctrl, pats)。

    缓存遍历拿到的元素取不了 pattern（AutomationElementMode_None），所以动作前用
    `ElementFromPoint` 取一次当前元素——单次跨进程调用，比重新逐节点遍历便宜得多。
    点上拿到的常常是目标控件的**子元素**（如按钮中心是 Text），所以沿父链向上找：
    第一个「身份匹配（名称相同或矩形 IoU≥0.6）且带所需 pattern」的元素就是目标。
    身份对不上就返回 (None, {})，上层据此回退慢路径——宁可慢，不能打错目标。
    """
    from ctypes import wintypes

    _core, iuia = _iuia()
    try:
        el = iuia.ElementFromPoint(wintypes.POINT(int(x), int(y)))
        if not el:
            return None, {}
        ctrl = _impl().Control.CreateControlFromElement(el)
    except Exception:
        return None, {}
    for _ in range(4):  # 最多向上找 3 层父控件
        if ctrl is None:
            break
        try:
            rect = _rect_of(ctrl)
            name = (ctrl.Name or "").strip()
        except Exception:
            return None, {}
        # pattern 一律探测（SetValue 路径必须知道有没有 value）；
        # 「是否要求带动作 pattern」由 want_action 决定——这两件事混在一起写过一次 bug：
        # want_action=False 时返回空 pats，SetValue 被误判成「目标不支持 ValuePattern」。
        pats = _patterns_of(ctrl)
        has_action = any(
            k in pats for k in ("invoke", "toggle", "expandcollapse", "selectionitem")
        )
        if (has_action or not want_action) and _same_target(
            {"name": name, "rect": rect or (0, 0, 0, 0)}, expect_name, expect_rect
        ):
            return ctrl, pats
        try:
            ctrl = ctrl.GetParentControl()
        except Exception:
            break
    return None, {}


def _resolve_action_control(hit: dict, hwnd: int, win_rect, expect_name: str,
                            expect_rect, want_action: bool = True):
    """把「遍历命中的条目」变成可操作的 Control，返回 (ctrl, pats)。

    逐节点路径的元素自带 Control，直接用；
    缓存路径的元素没有完整引用，改用点定位；点定位不成功则回退一次逐节点重扫。
    """
    if hit.get("ctrl") is not None:
        return hit["ctrl"], hit["pats"]
    name = expect_name or hit.get("name", "")
    rect = expect_rect or hit.get("rect")
    ctrl, pats = _control_at_point(
        hit["rect"][0] + (hit["rect"][2] - hit["rect"][0]) // 2,
        hit["rect"][1] + (hit["rect"][3] - hit["rect"][1]) // 2,
        name, rect, want_action=want_action,
    )
    if ctrl is not None:
        return ctrl, pats
    try:
        fresh = _collect_controls_per_node(hwnd, win_rect)["controls"]
    except Exception:
        return None, {}
    hit2 = _nearest(fresh, hit["rect"][0] + (hit["rect"][2] - hit["rect"][0]) // 2,
                    hit["rect"][1] + (hit["rect"][3] - hit["rect"][1]) // 2,
                    require_action=want_action)
    if hit2 is None or not _same_target(hit2, name, rect):
        return None, {}
    return hit2["ctrl"], hit2["pats"]


# ── 控件遍历 ────────────────────────────────────────────────────

def _rect_of(ctrl) -> Optional[tuple[int, int, int, int]]:
    """读 BoundingRectangle，兼容 Rect 对象与元组两种返回。无效/离屏返回 None。"""
    try:
        if ctrl.IsOffscreen:
            return None
        rect = ctrl.BoundingRectangle
        if hasattr(rect, "left"):
            l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
        else:
            l, t, r, b = rect[0], rect[1], rect[2], rect[3]
        l, t, r, b = int(l), int(t), int(r), int(b)
        if r <= l or b <= t:
            return None
        return (l, t, r, b)
    except Exception:
        return None


def _patterns_of(ctrl) -> dict:
    """探测控件支持的动作 pattern（每个 Get 调用都可能因控件失效抛异常）。"""
    pats: dict = {}
    for name, getter in (
        ("invoke", "GetInvokePattern"),
        ("toggle", "GetTogglePattern"),
        ("value", "GetValuePattern"),
        ("expandcollapse", "GetExpandCollapsePattern"),
        ("selectionitem", "GetSelectionItemPattern"),
    ):
        try:
            if getattr(ctrl, getter)():
                pats[name] = True
        except Exception:
            continue
    return pats


# ControlType ID → 卡片语义类型。
#
# 用标准 ID 常量（UIA_*ControlTypeId，跨版本稳定）而不是从库上 getattr：
# 缓存遍历拿到的是裸元素，只能拿到 ControlType 数值，两条路径必须共用同一张表，
# 否则缓存路径和逐节点路径会给出不同的 type（已踩过：库的常量名带 Control 后缀）。
_CTYPE_TO_TYPE = {
    50000: "button",       # UIA_ButtonControlTypeId
    50004: "input",        # UIA_EditControlTypeId
    50005: "link",         # UIA_HyperlinkControlTypeId
    50011: "menu",         # UIA_MenuItemControlTypeId
    50002: "checkbox",     # UIA_CheckBoxControlTypeId
    50013: "radio",        # UIA_RadioButtonControlTypeId
    50003: "combobox",     # UIA_ComboBoxControlTypeId
    50007: "listitem",     # UIA_ListItemControlTypeId
    50019: "tab",          # UIA_TabItemControlTypeId
    50024: "treeitem",     # UIA_TreeItemControlTypeId
    50020: "text",         # UIA_TextControlTypeId
    50006: "icon",         # UIA_ImageControlTypeId
}
_CTYPE_TITLE_BAR = 50037  # UIA_TitleBarControlTypeId


def _type_name_by_id(ctype) -> str:
    """ControlType 数值 → 卡片语义类型。

    注意：DocumentControl(50030) **不**映射成 input——文档容器不是输入框，
    误映射会让它绕过 pattern 判定白占卡片槽位（评审指出）。
    """
    return _CTYPE_TO_TYPE.get(ctype, "control")


def _type_name(uia, ctype: int) -> str:
    """逐节点路径用的包装（库的 ControlType 数值与标准 ID 相同，共用同一张表）。"""
    return _type_name_by_id(ctype)


# 即使没有任何动作 pattern 也保留的语义类型（可交互性高）
_KEEP_TYPES = {
    "button", "input", "link", "menu", "checkbox", "radio",
    "combobox", "listitem", "tab", "treeitem",
}


def _raw_rect(ctrl) -> Optional[tuple[int, int, int, int]]:
    """读原始 BoundingRectangle（不做 offscreen 过滤，用于根窗口矩形校验）。"""
    try:
        rect = ctrl.BoundingRectangle
        if hasattr(rect, "left"):
            l, t, r, b = rect.left, rect.top, rect.right, rect.bottom
        else:
            l, t, r, b = rect[0], rect[1], rect[2], rect[3]
        l, t, r, b = int(l), int(t), int(r), int(b)
        return (l, t, r, b) if r > l and b > t else None
    except Exception:
        return None


def _window_rect(hwnd: int) -> Optional[tuple]:
    try:
        import win32gui

        return tuple(win32gui.GetWindowRect(hwnd))
    except Exception:
        return None


def _intersects(a, b) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def _roots_for(hwnd: int, win_rect=None) -> list:
    """定位 UIA 遍历根，三级降级：

    1) `ControlFromHandle(hwnd)` 有子节点 → 直接用（Win32/WinForms/WPF/多数 UWP 帧）；
    2) 空壳：内容在**子窗口**里（UWP 的 CoreWindow 是跨进程子窗口）→ 逐个试子窗口句柄；
    3) 仍无：桌面根按 PID 过滤，**并且必须与目标窗口矩形相交**。
       第 3 级的矩形校验不是保险丝而是必需品：同个宿主进程（ApplicationFrameHost）
       下可能挂着多个 UWP 窗口，只按 PID 过滤会把无关窗口的控件混进目标窗口——
       实测查 Nahimic 帧却收集到「设置」的控件，而且 invoke_at/set_value_at 复用
       同一函数，会把动作打到无关窗口上。
    """
    uia = _u()
    shell = None
    try:
        shell = uia.ControlFromHandle(hwnd)
        if shell is not None and shell.GetChildren():
            return [shell]
    except Exception:
        pass

    # 2) 子窗口探测（UWP CoreWindow）
    roots: list = []
    try:
        import win32gui

        children: list = []
        win32gui.EnumChildWindows(hwnd, lambda h, _l: children.append(h) or True, None)
        for ch in children[:24]:
            try:
                c = uia.ControlFromHandle(ch)
                if c is not None and c.GetChildren():
                    roots.append(c)
            except Exception:
                continue
    except Exception as e:
        logger.debug("UIA 子窗口探测失败: %s", e)
    if roots:
        return roots

    # 3) 桌面根按 PID + 矩形相交过滤
    if win_rect is None:
        win_rect = _window_rect(hwnd)
    roots = []
    try:
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        root = uia.GetRootControl()
        for child in root.GetChildren():
            try:
                if child.ProcessId != pid:
                    continue
                cref = _raw_rect(child)
                if win_rect and cref and not _intersects(cref, win_rect):
                    continue  # 同进程的其它窗口（宿主下的另一个 UWP 窗口）
                roots.append(child)
            except Exception:
                continue
    except Exception as e:
        logger.debug("UIA 桌面根遍历失败: %s", e)
    if roots:
        logger.debug("UIA 空壳回退命中 %d 个根（hwnd=%s）", len(roots), hwnd)
        return roots
    return [shell] if shell is not None else []


def _collect_controls(hwnd: int, win_rect=None, max_depth: int = _MAX_DEPTH,
                      max_count: int = _MAX_COUNT,
                      time_budget: float = _TIME_BUDGET) -> dict:
    """遍历窗口 UIA 子树（调度器）：优先子树缓存，不行就回退逐节点。

    两条路径返回同构结果，且都做了深度/数量/时间三重封顶与标题栏 chrome 过滤。
    """
    global _cache_supported
    if _cache_supported is not False:
        try:
            res = _collect_controls_cached(hwnd, win_rect, max_depth, max_count, time_budget)
            _cache_supported = True
            return res
        except Exception as e:
            _cache_supported = False
            # 带堆栈：缓存路径挂掉后会静默退回慢路径，若吞掉细节就永远查不出来
            logger.warning("UIA 子树缓存遍历失败，回退逐节点遍历: %s", e, exc_info=True)
    return _collect_controls_per_node(hwnd, win_rect, max_depth, max_count, time_budget)


def _collect_controls_per_node(hwnd: int, win_rect=None, max_depth: int = _MAX_DEPTH,
                               max_count: int = _MAX_COUNT,
                               time_budget: float = _TIME_BUDGET) -> dict:
    """逐节点遍历（回退路径）。深度/数量/时间三重封顶，标题栏 chrome 整棵跳过。

    返回 {"controls": [...], "chrome_skipped": int, "truncated": bool,
          "truncated_reason": str, "visited": int, "elapsed": float}。
    返回的 ctrl 引用只在当次调用内使用（同线程、短命），绝不跨调用持有。
    """
    uia = _u()
    out: list[dict] = []
    chrome_skipped = 0
    truncated = False
    reason = ""
    title_bar_type = getattr(uia.ControlType, "TitleBarControl", None)
    strip_bottom = None
    if win_rect:
        strip_bottom = win_rect[1] + _title_strip_height(hwnd)

    stack = [(c, 0) for c in _roots_for(hwnd, win_rect)]
    seen = 0
    t0 = time.monotonic()
    while stack:
        if seen >= max_count:
            truncated, reason = True, "max_count"
            break
        # 每轮都查时间预算：慢 provider 单节点就要 ~300ms，若隔 N 个节点才查一次，
        # 预算会被远远突破（实测 Bandizip 在“每 16 节点查一次”下跑了 4.85s）
        if time_budget and time.monotonic() - t0 > time_budget:
            truncated, reason = True, "time_budget"
            break
        ctrl, depth = stack.pop()
        seen += 1
        try:
            name = (ctrl.Name or "").strip()
            ctype = ctrl.ControlType
        except Exception:
            name, ctype = "", None
        # 标题栏子树：chrome 按钮/系统菜单，整棵跳过（不遍历其子节点）
        if title_bar_type is not None and ctype == title_bar_type:
            chrome_skipped += 1
            continue
        rect = _rect_of(ctrl) if ctype is not None else None
        if rect is not None:
            if strip_bottom is not None and _looks_like_chrome(name, rect, strip_bottom):
                chrome_skipped += 1
                continue
            pats = _patterns_of(ctrl)
            etype = _type_name(uia, ctype)
            if pats or etype in _KEEP_TYPES:
                out.append({
                    "ctrl": ctrl, "name": name,
                    "type": etype, "rect": rect, "pats": pats,
                })
        if depth < max_depth:
            try:
                for ch in reversed(ctrl.GetChildren()):
                    stack.append((ch, depth + 1))
            except Exception:
                pass
    return {"controls": out, "chrome_skipped": chrome_skipped,
            "truncated": truncated, "truncated_reason": reason,
            "visited": seen, "elapsed": round(time.monotonic() - t0, 2)}


def _title_strip_height(hwnd: int) -> int:
    """标题栏条带高度（按窗口 DPI 缩放）：150% DPI 下 48pt → 72px。"""
    dpi = 96
    try:
        dpi = int(ctypes.windll.user32.GetDpiForWindow(hwnd)) or 96
    except Exception:
        pass
    return int(round(_TITLE_STRIP_BASE_PX * dpi / 96.0))


def _looks_like_chrome(name: str, rect, strip_bottom: int) -> bool:
    """标题栏条带内、名称含 chrome 关键词的控件（UWP 没有 TitleBarControl，靠这条兜底）。"""
    if not name:
        return False
    if (rect[1] + rect[3]) // 2 > strip_bottom:
        return False
    low = name.lower().replace(" ", "")
    return any(k in low for k in _CHROME_KEYWORDS)


def _foreground_hwnd() -> Optional[int]:
    """当前系统前台窗口（用于如实回传「本次操作是否改变了前台焦点」）。"""
    try:
        import win32gui

        return win32gui.GetForegroundWindow()
    except Exception:
        return None


def _signature(controls: list[dict]) -> frozenset:
    """控件树签名（名称+类型的多重计数），**只统计带动作 pattern 的控件**。

    不带 pattern 的类型（如纯文本标签）排除在外：它们名字随计数器/时钟变化会造成
    假阳性（把无效果的点击报成 success）。仍会捕获菜单展开、对话框弹出、按钮出现，
    以及继承自相邻 Label 的输入框名变化（后者确实是真实界面变化）。
    """
    return frozenset(
        Counter((c["name"], c["type"]) for c in controls if c["pats"]).items()
    )


# ── 感知：元素卡片 ──────────────────────────────────────────────

def list_elements(hwnd: int, win_rect=None, limit: int = 18,
                  stats: Optional[dict] = None) -> list[dict]:
    """遍历窗口 UIA 树，返回元素卡片用的 element 列表（屏幕坐标，source="uia"）。

    limit 默认 18：卡片总槽位是 40，UIA 先上图不代表可以占满——
    OCR 文本行是 Agent 理解界面的上下文，全被 UIA 挤掉反而降信息量（评审指出）。
    stats（可选，传入则写入诊断）：visited / elapsed / truncated / truncated_reason /
      slow_mode（该应用被判定为慢 provider，已切低预算模式）。
    同步阻塞，约定在 desktop.run() 线程中调用。
    """
    if not available():
        return []
    minimized = _is_minimized(hwnd)
    ckey = _cache_key(hwnd)
    if not minimized:
        ts = _empty_cache.get(ckey)
        if ts is not None:
            if time.monotonic() - ts < _EMPTY_TTL:
                logger.debug("UIA 空树缓存命中（%s），跳过遍历", ckey[1] or ckey[0])
                if stats is not None:
                    stats.update({"cached_empty": True, "visited": 0, "elapsed": 0.0})
                return []
            _empty_cache.pop(ckey, None)

    # 慢 provider（实测单次调用 ~300ms 的应用）自动切低预算模式，避免每次 look 白等十几秒
    slow_ts = _slow_cache.get(ckey)
    slow_mode = False
    if slow_ts is not None:
        if time.monotonic() - slow_ts < _SLOW_TTL:
            slow_mode = True
        else:
            _slow_cache.pop(ckey, None)

    t0 = time.monotonic()
    collected = _collect_controls(
        hwnd, win_rect=win_rect,
        max_depth=_MAX_DEPTH_SLOW if slow_mode else _MAX_DEPTH,
        time_budget=_TIME_BUDGET_SLOW if slow_mode else _TIME_BUDGET,
    )
    cost = time.monotonic() - t0
    if collected["truncated"]:
        # 大 Chrome/Electron 树或慢 provider：卡片可能不含真正的目标控件——如实上报
        logger.info(
            "UIA 控件树截断（原因=%s，访问 %d 节点，耗时 %.2fs，chrome 过滤 %d 个）",
            collected["truncated_reason"], collected["visited"], cost,
            collected["chrome_skipped"],
        )
        if collected["truncated_reason"] == "time_budget" and not slow_mode:
            _slow_cache[ckey] = time.monotonic()
            logger.info("UIA 慢 provider 已记住（%s）：后续 %d 秒内改用低预算模式",
                        ckey[1] or ckey[0], int(_SLOW_TTL))
    if stats is not None:
        stats.update({
            "visited": collected["visited"],
            "elapsed": collected["elapsed"],
            "chrome_skipped": collected["chrome_skipped"],
            "truncated": collected["truncated"],
            "truncated_reason": collected["truncated_reason"],
            "slow_mode": slow_mode,
            "cached": bool(collected.get("cached")),
            "fetch_sec": collected.get("fetch_sec"),
        })
    elements = []
    try:
        from . import desktop

        sb = desktop.screen_bounds()
    except Exception:
        sb = None
    for c in collected["controls"]:
        l, t, r, b = c["rect"]
        if win_rect:
            # 与窗口矩形无交集的控件（屏外/隐藏面板）不上卡片
            if r <= win_rect[0] or l >= win_rect[2] or b <= win_rect[1] or t >= win_rect[3]:
                continue
        # 真实环境实测：Chromium 根容器会报超出屏幕的矩形（右边界 2911 > 屏宽 2560），
        # 拿去画框/做去重判据都会错，必须先钳制到「窗口 ∩ 屏幕」范围
        cl, ct, cr, cb = l, t, r, b
        if win_rect:
            cl, ct = max(l, win_rect[0]), max(t, win_rect[1])
            cr, cb = min(r, win_rect[2]), min(b, win_rect[3])
        if sb:
            cl, ct = max(cl, sb[0]), max(ct, sb[1])
            cr, cb = min(cr, sb[2]), min(cb, sb[3])
        if cr - cl <= 2 or cb - ct <= 2:
            continue  # 钳制后没有有效区域（本来就在屏外）
        el = {
            "name": c["name"][:60] or c["type"],
            "type": c["type"],
            "x": (cl + cr) // 2, "y": (ct + cb) // 2,
            "left": cl, "top": ct, "right": cr, "bottom": cb,
            "has_icon": False, "source": "uia",
            "uia_patterns": sorted(c["pats"].keys()),
            "_area": (cr - cl) * (cb - ct),
        }
        if (cl, ct, cr, cb) != (l, t, r, b):
            el["rect_clamped"] = True  # 如实告知：原始矩形超出窗口/屏幕，已钳制
        elements.append(el)

    # 排序：容器级元素（占窗口大部分面积）靠后——否则它靠“左上角靠前”抢到 e1 这种
    # 头号位置，截断时还会把真正的控件挤掉；其余按视觉阅读顺序（上→下、左→右）
    win_area = (
        float((win_rect[2] - win_rect[0]) * (win_rect[3] - win_rect[1])) if win_rect else 0.0
    )
    for el in elements:
        el["uia_container"] = bool(win_area and el["_area"] >= 0.6 * win_area)
    elements.sort(key=lambda e: (e["uia_container"], e["top"], e["left"]))
    for el in elements:
        el.pop("_area", None)
    if not elements and not minimized and cost > _SLOW_EMPTY_SEC:
        # 慢空树：缓存下来避免后续 look 白等（最小化窗口不入缓存，见函数内注释）
        _empty_cache[ckey] = time.monotonic()
        logger.debug("UIA 慢空树（%.2fs）已缓存 %s", cost, ckey[1] or ckey[0])
    return elements[:limit]

# ── 执行：后台动作 ──────────────────────────────────────────────

def _iou(a, b) -> float:
    """两个矩形的交并比（用于校验“重定位到的控件是否还是卡片里那一个”）。"""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - inter
    return inter / float(union) if union > 0 else 0.0


def _same_target(hit: dict, expect_name: str = "", expect_rect=None) -> bool:
    """重定位到的控件是否仍是卡片里记的那一个。

    两道判据取或：名称相同，或矩形 IoU ≥ 0.6。
    单靠名称会误拒（WinForms 输入框的可访问名会继承相邻 Label 的文本，随界面变），
    单靠矩形会误收（布局移动后同位置换成了别的控件）——所以两者取或，
    任一成立则认为是同一控件；都不成立就拒绝执行（交上层报 stale）。
    """
    if not expect_name and not expect_rect:
        return True  # 无可校验信息（直接调用）
    if expect_name and hit["name"] and expect_name == hit["name"]:
        return True
    if expect_rect:
        try:
            if _iou(hit["rect"], tuple(int(v) for v in expect_rect)) >= 0.6:
                return True
        except Exception:
            pass
    return False


def _nearest(controls: list[dict], x: int, y: int,
             require_action: bool = False) -> Optional[dict]:
    """中心距 (x,y) 最近的可交互控件。require_action=True 时要求带动作 pattern。

    平手时取**面积更小**的控件（更具体的那个），再平手取先出现者——
    旧实现用 `<=` 更新，密排 UI 里会系统性偏向靠后的控件。
    """
    best = None
    best_key = None
    max_d2 = _MAX_REMATCH_DIST * _MAX_REMATCH_DIST
    for c in controls:
        if require_action and not any(
            k in c["pats"] for k in ("invoke", "toggle", "expandcollapse", "selectionitem")
        ):
            continue
        l, t, r, b = c["rect"]
        cx, cy = (l + r) // 2, (t + b) // 2
        d2 = (cx - x) ** 2 + (cy - y) ** 2
        if d2 > max_d2:
            continue
        key = (d2, (r - l) * (b - t))
        if best_key is None or key < best_key:
            best_key, best = key, c
    return best


def _read_state(ctrl, pats: dict) -> Optional[dict]:
    """状态回读：ToggleState / ExpandCollapseState / IsSelected / Value。"""
    state: dict = {}
    try:
        if "toggle" in pats:
            state["toggle"] = int(ctrl.GetTogglePattern().ToggleState)
    except Exception:
        pass
    try:
        if "expandcollapse" in pats:
            state["expandcollapse"] = int(
                ctrl.GetExpandCollapsePattern().ExpandCollapseState
            )
    except Exception:
        pass
    try:
        if "selectionitem" in pats:
            state["selected"] = bool(ctrl.GetSelectionItemPattern().IsSelected)
    except Exception:
        pass
    return state or None


def invoke_at(hwnd: int, x: int, y: int, expect_name: str = "",
              expect_rect=None) -> dict:
    """后台点击：按优先级 Invoke > Toggle > ExpandCollapse > SelectionItem。

    expect_name/expect_rect：卡片里记的名称与矩形，用于**校验重定位到的控件是否
    已不是卡片里那一个**（界面翻页/布局变化后同坐标可能是别的控件）。
    不匹配时返回 {ok: False, mismatch: True}，上层据此报 stale 而不是将错就错。

    返回 {ok, pattern, name, state_before, state_after, state_changed, tree_changed,
          focus_before, focus_after, focus_changed}。
    同步阻塞，约定在 desktop.run() 线程中调用。不动鼠标；焦点是否被带走由
    focus_changed 如实回传（部分框架的控件在动作时会把窗口带到前台）。
    """
    if not available():
        return {"ok": False, "error": "uiautomation 不可用"}
    win_rect = _window_rect(hwnd)
    try:
        controls = _collect_controls(hwnd, win_rect)["controls"]
    except Exception as e:
        return {"ok": False, "error": f"UIA 遍历失败: {e}"}
    hit = _nearest(controls, x, y, require_action=True)
    if hit is None:
        return {"ok": False, "error": "该位置附近没有可后台操作的 UIA 控件"}
    if not _same_target(hit, expect_name, expect_rect):
        return {
            "ok": False, "mismatch": True,
            "error": (f"卡片元素已过期：该位置现在的控件是「{hit['name'] or hit['type']}」"
                      f"（{hit['type']}），与卡片记录的「{expect_name or '未命名'}」不符，"
                      f"请重新 look/scan_scene"),
        }
    # 缓存遍历的元素没有完整引用，这里解析成可操作的 Control（失败则回退逐节点重扫）
    ctrl, pats = _resolve_action_control(hit, hwnd, win_rect, expect_name, expect_rect,
                                         want_action=True)
    if ctrl is None:
        return {"ok": False, "error": "无法取得该位置的可操作控件（点定位与逐节点重扫均未命中）"}
    sig_before = _signature(controls)
    state_before = _read_state(ctrl, pats)
    focus_before = _foreground_hwnd()

    try:
        if "invoke" in pats:
            ctrl.GetInvokePattern().Invoke()
            pattern = "invoke"
        elif "toggle" in pats:
            ctrl.GetTogglePattern().Toggle()
            pattern = "toggle"
        elif "expandcollapse" in pats:
            # 语义对齐点击：已展开则折叠，已折叠则展开
            ep = ctrl.GetExpandCollapsePattern()
            if int(ep.ExpandCollapseState) == 1:  # Expanded
                ep.Collapse()
            else:
                ep.Expand()
            pattern = "expandcollapse"
        elif "selectionitem" in pats:
            ctrl.GetSelectionItemPattern().Select()
            pattern = "selectionitem"
        else:
            return {"ok": False, "error": "控件无可用动作 pattern"}
    except Exception as e:
        return {"ok": False, "error": f"UIA 动作执行失败: {e}"}

    time.sleep(0.2)  # 给界面一点响应时间再做回读/结构对比
    state_after = _read_state(ctrl, pats)
    focus_after = _foreground_hwnd()
    tree_changed = False
    try:
        tree_changed = _signature(_collect_controls(hwnd, win_rect)["controls"]) != sig_before
    except Exception:
        pass
    return {
        "ok": True,
        "pattern": pattern,
        "name": hit["name"],
        "state_before": state_before,
        "state_after": state_after,
        "state_changed": (
            state_before is not None and state_after is not None
            and state_before != state_after
        ),
        "tree_changed": tree_changed,
        "focus_before": focus_before,
        "focus_after": focus_after,
        "focus_changed": (
            focus_before is not None and focus_after is not None
            and focus_before != focus_after
        ),
    }


def set_value_at(hwnd: int, x: int, y: int, text: str, expect_name: str = "",
                 expect_rect=None) -> dict:
    """后台打字：ValuePattern.SetValue + 回读校验（不走剪贴板、不动鼠标）。

    expect_name/expect_rect：卡片里记的名称与矩形，用于校验重定位到的控件是否
    已不是卡片里那一个——界面翻页后同坐标换成另一个输入框时，回读校验会通过
    而内容是错的（评审指出的静默错写风险），所以必须先校验身份再写。

    返回 {ok, verified, value_before, value_after, focus_changed}。
    同步阻塞，desktop.run() 线程中调用。
    """
    if not available():
        return {"ok": False, "error": "uiautomation 不可用"}
    win_rect = _window_rect(hwnd)
    try:
        hit = _nearest(
            _collect_controls(hwnd, win_rect)["controls"], x, y, require_action=False
        )
    except Exception as e:
        return {"ok": False, "error": f"UIA 遍历失败: {e}"}
    # SetValue 只认带 ValuePattern 的控件
    if hit is not None and "value" not in hit["pats"]:
        hit = None
    if hit is None:
        return {"ok": False, "error": "该位置附近没有支持 ValuePattern 的控件"}
    if not _same_target(hit, expect_name, expect_rect):
        return {
            "ok": False, "mismatch": True,
            "error": (f"卡片元素已过期：该位置现在的输入控件是「{hit['name'] or hit['type']}」，"
                      f"与卡片记录的「{expect_name or '未命名'}」不符，请重新 look/scan_scene"),
        }
    ctrl, pats = _resolve_action_control(hit, hwnd, win_rect, expect_name, expect_rect,
                                         want_action=False)
    if ctrl is None:
        return {"ok": False, "error": "无法取得该位置的可操作控件（点定位与逐节点重扫均未命中）"}
    if "value" not in pats:
        return {"ok": False, "error": "目标控件不支持 ValuePattern"}
    focus_before = _foreground_hwnd()
    try:
        vp = ctrl.GetValuePattern()
        value_before = vp.Value
        vp.SetValue(text)
        time.sleep(0.2)
        value_after = ctrl.GetValuePattern().Value
        focus_after = _foreground_hwnd()
        return {
            "ok": True,
            "name": hit["name"],
            "value_before": value_before,
            "value_after": value_after,
            "verified": value_after == text,
            "focus_before": focus_before,
            "focus_after": focus_after,
            "focus_changed": (
                focus_before is not None and focus_after is not None
                and focus_before != focus_after
            ),
        }
    except Exception as e:
        return {"ok": False, "error": f"SetValue 失败: {e}"}
