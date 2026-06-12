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


def _err(msg: str) -> str:
    return json.dumps({"ok": False, "error": msg}, ensure_ascii=False)


# ── 9 个模块级 handler（普通函数，AstrBot 会通过 functools.partial 注入 star_cls） ──

async def _desk_state_handler(self, event, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_state))

async def _desk_click_handler(self, event, id: int, button: str = "left", double: bool = False, hover: bool = False, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_click, id, button, double, hover))

async def _desk_type_handler(self, event, id: int, text: str, line: int = None, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_type, id, text, line))

async def _desk_press_handler(self, event, keys: list, action: str = "press", **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_press, keys, action))

async def _desk_drag_handler(self, event, from_id: int, to_id: int = None, to_x: int = None, to_y: int = None, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_drag, from_id, to_id, to_x, to_y))

async def _desk_scroll_handler(self, event, id: int, direction: str, amount: int = 3, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_scroll, id, direction, amount))

async def _desk_select_handler(self, event, id: int, start: int, end: int, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_select, id, start, end))

async def _desk_window_handler(self, event, action: str, hwnd: int = None, x: int = None, y: int = None, w: int = None, h: int = None, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_window, action, hwnd, x, y, w, h))

async def _desk_screenshot_handler(self, event, annotate: bool = False, **kwargs) -> str:
    return _ok(await asyncio.to_thread(desk_screenshot, annotate))


class DeskHandPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)

        tools = [
            FunctionTool(
                name="desk_state",
                description="采集当前活跃窗口的控件树，返回结构化 JSON。每个控件含 id/role/name/value/rect/enabled。target 参数可按窗口名过滤（如 target=\"QQ\" 仅扫描 QQ 窗口），大幅减少输出 token。",
                parameters={
                    "type": "object",
                    "properties": {
                        "target": {"type": "string", "description": "窗口名模糊匹配（不区分大小写），不传则扫描全桌面"}
                    },
                    "required": []
                },
                handler=_desk_state_handler,
            ),
            FunctionTool(
                name="desk_click",
                description="点击或悬停指定控件。id 为 desk_state 返回的控件 id。button=left/right/middle，double=True 双击，hover=True 悬停不移开。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "控件 id"},
                        "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "鼠标按键，默认 left"},
                        "double": {"type": "boolean", "description": "是否双击，默认 false"},
                        "hover": {"type": "boolean", "description": "悬停不移开，默认 false"},
                    },
                    "required": ["id"],
                },
                handler=_desk_click_handler,
            ),
            FunctionTool(
                name="desk_type",
                description="向控件输入文本，可指定行号修改单行内容。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "控件 id"},
                        "text": {"type": "string", "description": "要输入的文本"},
                        "line": {"type": "integer", "description": "可选，行号（修改该行内容）"},
                    },
                    "required": ["id", "text"],
                },
                handler=_desk_type_handler,
            ),
            FunctionTool(
                name="desk_press",
                description="发送键盘按键组合。keys 如 ['Ctrl', 'c']，action=press/hold/release。",
                parameters={
                    "type": "object",
                    "properties": {
                        "keys": {"type": "array", "items": {"type": "string"}, "description": "按键列表"},
                        "action": {"type": "string", "enum": ["press", "hold", "release"], "description": "动作类型，默认 press"},
                    },
                    "required": ["keys"],
                },
                handler=_desk_press_handler,
            ),
            FunctionTool(
                name="desk_drag",
                description="拖拽操作：from_id 拖到 to_id 或 (to_x, to_y) 坐标。",
                parameters={
                    "type": "object",
                    "properties": {
                        "from_id": {"type": "integer", "description": "起始控件 id"},
                        "to_id": {"type": "integer", "description": "目标控件 id（与坐标二选一）"},
                        "to_x": {"type": "integer", "description": "目标 X 坐标"},
                        "to_y": {"type": "integer", "description": "目标 Y 坐标"},
                    },
                    "required": ["from_id"],
                },
                handler=_desk_drag_handler,
            ),
            FunctionTool(
                name="desk_scroll",
                description="对指定控件滚动。direction=up/down，amount 为滚动量（默认 3）。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "控件 id"},
                        "direction": {"type": "string", "enum": ["up", "down"], "description": "滚动方向"},
                        "amount": {"type": "integer", "description": "滚动量，默认 3"},
                    },
                    "required": ["id", "direction"],
                },
                handler=_desk_scroll_handler,
            ),
            FunctionTool(
                name="desk_select",
                description="选中指定控件内第 start 到第 end 个字符。",
                parameters={
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "控件 id"},
                        "start": {"type": "integer", "description": "起始字符位置"},
                        "end": {"type": "integer", "description": "结束字符位置"},
                    },
                    "required": ["id", "start", "end"],
                },
                handler=_desk_select_handler,
            ),
            FunctionTool(
                name="desk_window",
                description="窗口管理：min/max/restore/close/focus/set_topmost/move/resize。",
                parameters={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["min", "max", "restore", "close", "focus", "set_topmost", "move", "resize"], "description": "窗口操作"},
                        "hwnd": {"type": "integer", "description": "窗口句柄（可选，默认当前窗口）"},
                        "x": {"type": "integer", "description": "move/resize 时的 X"},
                        "y": {"type": "integer", "description": "move/resize 时的 Y"},
                        "w": {"type": "integer", "description": "resize 时的宽度"},
                        "h": {"type": "integer", "description": "resize 时的高度"},
                    },
                    "required": ["action"],
                },
                handler=_desk_window_handler,
            ),
            FunctionTool(
                name="desk_screenshot",
                description="截取当前屏幕，保存 PNG 文件，返回路径、分辨率、文件大小。可选标注控件边框和 id。",
                parameters={
                    "type": "object",
                    "properties": {
                        "annotate": {"type": "boolean", "description": "是否标注控件边框和 id，默认 false"},
                    },
                    "required": [],
                },
                handler=_desk_screenshot_handler,
            ),
        ]

        context.add_llm_tools(*tools)
        logger.info(f"DeskHand 插件已加载 — 注册了 {len(tools)} 个 LLM Tool")
