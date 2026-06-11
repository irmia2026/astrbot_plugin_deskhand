"""
main.py — AstrBot Star 插件入口。

注册 9 个 LLM Tool，供 LLM Agent 调用。
所有 Tool 内部调用同步阻塞的 UIA/win32 API，使用 asyncio.to_thread 避免阻塞事件循环。
"""

import asyncio
import logging
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api.all import *

from .tools.desk_state import desk_state
from .tools.desk_click import desk_click
from .tools.desk_type import desk_type
from .tools.desk_press import desk_press
from .tools.desk_drag import desk_drag
from .tools.desk_scroll import desk_scroll
from .tools.desk_select import desk_select
from .tools.desk_window import desk_window
from .tools.desk_screenshot import desk_screenshot

logger = logging.getLogger("deskhand")


@register("astrbot_plugin_deskhand", "opencode", "精准 Windows GUI 操控插件", "1.0.0")
class DeskHandPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        logger.info("DeskHand 插件已加载")

    # ── 9 个 LLM Tool ───────────────────────────────────────────

    @tool("desk_state")
    async def tool_desk_state(self, event: AstrMessageEvent) -> str:
        """采集当前活跃窗口的控件树，返回结构化 JSON。"""
        return await asyncio.to_thread(desk_state)

    @tool("desk_click")
    async def tool_desk_click(self, event: AstrMessageEvent, id: int,
                              button: str = "left", double: bool = False,
                              hover: bool = False) -> str:
        """点击或悬停指定控件。"""
        return await asyncio.to_thread(desk_click, id, button, double, hover)

    @tool("desk_type")
    async def tool_desk_type(self, event: AstrMessageEvent, id: int,
                             text: str, line: int = None) -> str:
        """向控件输入文本，可指定行号修改单行内容。"""
        return await asyncio.to_thread(desk_type, id, text, line)

    @tool("desk_press")
    async def tool_desk_press(self, event: AstrMessageEvent,
                              keys: list, action: str = "press") -> str:
        """发送键盘按键组合。"""
        return await asyncio.to_thread(desk_press, keys, action)

    @tool("desk_drag")
    async def tool_desk_drag(self, event: AstrMessageEvent, from_id: int,
                             to_id: int = None, to_x: int = None,
                             to_y: int = None) -> str:
        """拖拽操作：从控件拖到另一个控件或指定坐标。"""
        return await asyncio.to_thread(desk_drag, from_id, to_id, to_x, to_y)

    @tool("desk_scroll")
    async def tool_desk_scroll(self, event: AstrMessageEvent, id: int,
                               direction: str, amount: int = 3) -> str:
        """对指定控件滚动。"""
        return await asyncio.to_thread(desk_scroll, id, direction, amount)

    @tool("desk_select")
    async def tool_desk_select(self, event: AstrMessageEvent, id: int,
                               start: int, end: int) -> str:
        """选中指定控件内第 start 到第 end 个字符。"""
        return await asyncio.to_thread(desk_select, id, start, end)

    @tool("desk_window")
    async def tool_desk_window(self, event: AstrMessageEvent, action: str,
                               hwnd: int = None, x: int = None, y: int = None,
                               w: int = None, h: int = None) -> str:
        """窗口管理操作：min/max/restore/close/focus/set_topmost/move/resize。"""
        return await asyncio.to_thread(desk_window, action, hwnd, x, y, w, h)

    @tool("desk_screenshot")
    async def tool_desk_screenshot(self, event: AstrMessageEvent,
                                   annotate: bool = False) -> str:
        """截图，可选标注控件边框和 id。"""
        return await asyncio.to_thread(desk_screenshot, annotate)
