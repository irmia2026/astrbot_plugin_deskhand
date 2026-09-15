"""
main.py — DeskHand v2：视觉方案桌面操控插件（AstrBot Star 入口）。

与 v1 的区别：全面转向视觉方案，不再使用 UIA 控件树。
- 定位：元素记忆 → 本地 OCR → VL 网格漏斗（三级降级）；
- 执行：win32 键鼠（UNICODE 文本注入，支持中文）；
- 验证：ImageChops 图像 diff（确定性信号，C 速度）；
- VL 降级链：优先复用同时安装的 irmia_vision 插件，否则用本插件配置/AstrBot 已保存模型。
"""

from __future__ import annotations

import os

from astrbot.api import logger, star

from . import tools as _tools
from .engine import locate as _locate
from .engine import memory as _memory_mod
from .engine import vl as _vl

_DEFAULT_CONFIG = {
    "ocr_enabled": True,
    "ocr_multiscale": "auto",
    "uia_enabled": True,
    "memory_enabled": True,
    "hover_verify": True,
    "max_zoom": 2,
    "post_action_wait": 0.4,
}


class DeskHandPlugin(star.Star):
    """DeskHand v2 — 视觉方案桌面操控"""

    def __init__(self, context: star.Context, config: dict = None) -> None:
        super().__init__(context)
        self.context = context

        cfg = dict(_DEFAULT_CONFIG)
        if isinstance(config, dict):
            # AstrBot 分节配置：展开已知 section（vl_model 等嵌套 dict 保持原样）
            for section in ("VL 模型配置", "定位与行为"):
                sec = config.get(section)
                if isinstance(sec, dict):
                    cfg.update(sec)
            # 兼容无分节的平铺配置
            for k, v in config.items():
                if k not in ("VL 模型配置", "定位与行为"):
                    cfg[k] = v

        # VL 层：配置 + AstrBot context（provider 发现）
        _vl.setup(cfg, context)
        _tools.setup(cfg)

        # 元素记忆库（SQLite，放插件数据目录）
        self._memory = None
        if cfg.get("memory_enabled", True):
            try:
                try:
                    from astrbot.api.star import StarTools
                    data_dir = str(StarTools.get_data_dir())
                except Exception:
                    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
                db_path = os.path.join(data_dir, "deskhand_memory.db")
                self._memory = _memory_mod.ElementMemory(db_path)
                _locate.set_memory(self._memory)
            except Exception as e:
                logger.warning(f"deskhand: 记忆库初始化失败（降级为无记忆模式）: {e}")

        tools = _tools.register_all()
        context.add_llm_tools(*tools)
        # 修正 handler_module_path，保证插件卸载/重载时工具能被正确清理
        for t in tools:
            t.handler_module_path = __name__

        ocr_state = "可用" if self._ocr_available() else "不可用（安装 winsdk 或 rapidocr-onnxruntime 可开启）"
        uia_state = "可用" if self._uia_available() else "未安装（pip install uiautomation 可开启后台操作）"
        logger.info(
            f"DeskHand v2 已加载 — {len(tools)} 个工具 | VL: {'可用' if _vl.vl_available() else '未配置'} | "
            f"OCR: {ocr_state} | UIA: {uia_state} | 记忆库: {'开启' if self._memory else '关闭'}"
        )

    @staticmethod
    def _uia_available() -> bool:
        try:
            from .engine import uia
            return uia.available()
        except Exception:
            return False

    @staticmethod
    def _ocr_available() -> bool:
        try:
            from .engine import ocr
            return ocr.available()
        except Exception:
            return False

    async def terminate(self) -> None:
        _locate.set_memory(None)  # 先解除全局引用，避免后续调用打到已关闭的连接
        if self._memory is not None:
            try:
                self._memory.close()
            except Exception:
                pass
            self._memory = None
        try:
            from .engine import desktop as _desktop
            _desktop.shutdown()
        except Exception:
            pass
