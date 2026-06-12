"""
main.py — AstrBot Star 插件入口。

注册 9 个 LLM Tool，供 LLM Agent 调用。
所有 Tool 内部调用同步阻塞的 UIA/win32 API，使用 asyncio.to_thread 避免阻塞事件循环。
"""

import asyncio
import json
import logging

from astrbot.api import logger, FunctionTool
from astrbot.api.star import Context, Star

from .tools.desk_state import desk_state
from .tools.desk_click import desk_click
from .tools.desk_type import desk_type
from .tools.desk_press import desk_press
from .tools.desk_drag import desk_drag
from .tools.desk_scroll import desk_scroll
from .tools.desk_select import desk_select
from .tools.desk_window import desk_window
from .tools.desk_screenshot import desk_screenshot


def _ok(data) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


# ── 9 个模块级 handler（普通函数，AstrBot 会通过 functools.partial 注入 star_cls） ──

async def _desk_state_handler(self, event, target: str = None, mode: str = None, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_state, target=target, mode=mode))

async def _desk_click_handler(self, event, id: int, button: str = "left", double: bool = False, hover: bool = False, verify: str = "full", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_click, id, button, double, hover, verify=verify))

async def _desk_type_handler(self, event, id: int, text: str, line: int = None, verify: str = "full", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_type, id, text, line, verify=verify))

async def _desk_press_handler(self, event, keys: list, action: str = "press", verify: str = "full", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_press, keys, action, verify=verify))

async def _desk_drag_handler(self, event, from_id: int, to_id: int = None, to_x: int = None, to_y: int = None, verify: str = "full", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_drag, from_id, to_id, to_x, to_y, verify=verify))

async def _desk_scroll_handler(self, event, id: int, direction: str, amount: int = 3, verify: str = "full", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_scroll, id, direction, amount, verify=verify))

async def _desk_select_handler(self, event, id: int, start: int, end: int, verify: str = "full", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_select, id, start, end, verify=verify))

async def _desk_window_handler(self, event, action: str, hwnd: int = None, x: int = None, y: int = None, w: int = None, h: int = None, verify: str = "full", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_window, action, hwnd, x, y, w, h, verify=verify))

async def _desk_screenshot_handler(self, event, annotate: bool = False, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_screenshot, annotate))


class DeskHandPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        tools = [
            FunctionTool(
                name="desk_state",
                description="扫描窗口控件树。target=窗口名过滤；mode=interactive只返交互控件/coords只返坐标。",
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "窗口名模糊匹配，不传则扫全桌面"},
                        "mode": {"type": "string", "enum": ["interactive", "coords"], "description": "interactive过滤结构节点，coords仅窗口rect"}
                    },
                    "required": []
                },
                handler=_desk_state_handler,
            ),
            FunctionTool(
                name="desk_click",
                description="点击/悬停控件。verify=light跳过像素diff。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"]},
                        "double": {"type": "boolean"},
                        "hover": {"type": "boolean"},
                        "verify": {"type": "string", "enum": ["full", "light", "none"], "description": "full截屏对比/light仅前景/none跳过"},
                    },
                    "required": ["id"],
                },
                handler=_desk_click_handler,
            ),
            FunctionTool(
                name="desk_type",
                description="向控件输入文本。verify=light跳过像素diff。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "text": {"type": "string"},
                        "line": {"type": "integer"},
                        "verify": {"type": "string", "enum": ["full", "light", "none"], "description": "full截屏对比/light仅前景/none跳过"},
                    },
                    "required": ["id", "text"],
                },
                handler=_desk_type_handler,
            ),
            FunctionTool(
                name="desk_press",
                description="发送按键组合。verify=light跳过像素diff。",
                parameters={
                    "type": "object",
                    "properties": {
                        "keys": {"type": "array", "items": {"type": "string"}},
                        "action": {"type": "string", "enum": ["press", "hold", "release"]},
                        "verify": {"type": "string", "enum": ["full", "light", "none"], "description": "full截屏对比/light仅前景/none跳过"},
                    },
                    "required": ["keys"],
                },
                handler=_desk_press_handler,
            ),
            FunctionTool(
                name="desk_drag",
                description="拖拽：from_id到to_id或(to_x,to_y)。verify=light跳过像素diff。",
                parameters={
                    "type": "object",
                    "properties": {
                        "from_id": {"type": "integer"},
                        "to_id": {"type": "integer"},
                        "to_x": {"type": "integer"},
                        "to_y": {"type": "integer"},
                        "verify": {"type": "string", "enum": ["full", "light", "none"]},
                    },
                    "required": ["from_id"],
                },
                handler=_desk_drag_handler,
            ),
            FunctionTool(
                name="desk_scroll",
                description="对控件滚动。verify=light跳过像素diff。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                        "amount": {"type": "integer"},
                        "verify": {"type": "string", "enum": ["full", "light", "none"]},
                    },
                    "required": ["id", "direction"],
                },
                handler=_desk_scroll_handler,
            ),
            FunctionTool(
                name="desk_select",
                description="选中控件内start到end字符。verify=light跳过像素diff。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "start": {"type": "integer"},
                        "end": {"type": "integer"},
                        "verify": {"type": "string", "enum": ["full", "light", "none"]},
                    },
                    "required": ["id", "start", "end"],
                },
                handler=_desk_select_handler,
            ),
            FunctionTool(
                name="desk_window",
                description="窗口管理：min/max/restore/close/focus/set_topmost/move/resize。verify=light跳过像素diff。",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["min", "max", "restore", "close", "focus", "set_topmost", "move", "resize"]},
                        "hwnd": {"type": "integer"},
                        "x": {"type": "integer"},
                        "y": {"type": "integer"},
                        "w": {"type": "integer"},
                        "h": {"type": "integer"},
                        "verify": {"type": "string", "enum": ["full", "light", "none"]},
                    },
                    "required": ["action"],
                },
                handler=_desk_window_handler,
            ),
            FunctionTool(
                name="desk_screenshot",
                description="截图保存PNG，返回路径。annotate=True标注控件框。",
                parameters={
                    "type": "object",
                    "properties": {
                        "annotate": {"type": "boolean"},
                    },
                    "required": [],
                },
                handler=_desk_screenshot_handler,
            ),
        ]

        context.add_llm_tools(*tools)
        logger.info(f"DeskHand 插件已加载 — 注册了 {len(tools)} 个 LLM Tool")
