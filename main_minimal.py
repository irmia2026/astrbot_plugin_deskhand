"""
main.py — AstrBot Star 插件入口（极简测试版）。
"""

from astrbot.api import logger, FunctionTool
from astrbot.api.star import Context, Star


class DeskHandPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        logger.info("DeskHand 极简版已加载（无 Tool）")
