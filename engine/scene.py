"""
scene.py — 场景快照注册中心：元素编号（e1..eN）→ 坐标的映射。

设计目标：让 Agent 用编号引用元素（click(element="e3")），
不再关心坐标/名称/换算。look/scan_scene 在返回前调用 register()，
click 等动作工具通过 resolve() 取回元素。

快照 TTL 120 秒：界面是易变的，过期快照强制重新 look。
注册时可为每个元素附带图像签名（crop_sig），供点击前现场校验——
弹窗遮挡/布局移动时 click 能发现并自愈/报错，而不是盲点。
"""

from __future__ import annotations

import time
from typing import Optional

_TTL = 120.0

_snapshot: dict = {"ts": 0.0, "window": "", "elements": []}


def register(elements: list[dict], window_title: str = "",
             shot=None, origin: tuple = (0, 0)) -> list[dict]:
    """注册一组元素并分配编号 e1..eN。返回带 id 的元素列表。

    元素字段：name/type/x/y/has_icon/source，此处补充 id；
    传入 shot 时为每个元素计算 64px 局部图像签名 crop_sig（现场校验用）。
    """
    from . import memory as mem

    numbered = []
    for i, el in enumerate(elements):
        item = dict(el)
        item["id"] = f"e{i + 1}"
        if shot is not None:
            try:
                item["crop_sig"] = mem.crop_signature(
                    shot, int(el["x"] - origin[0]), int(el["y"] - origin[1])
                )
            except Exception:
                item["crop_sig"] = ""
        numbered.append(item)
    _snapshot["ts"] = time.monotonic()
    _snapshot["window"] = window_title or ""
    _snapshot["elements"] = numbered
    return numbered


def resolve(element_id: str) -> Optional[dict]:
    """按编号取元素。过期或不存在返回 None。"""
    if not element_id:
        return None
    if time.monotonic() - _snapshot["ts"] > _TTL:
        return None
    eid = str(element_id).strip().lower()
    for el in _snapshot["elements"]:
        if el.get("id", "").lower() == eid:
            return el
    return None


def age() -> float:
    """快照年龄（秒）；无快照返回 inf。"""
    if not _snapshot["ts"]:
        return float("inf")
    return time.monotonic() - _snapshot["ts"]


def current_window() -> str:
    """快照所属窗口标题（过期返回空串）。"""
    if time.monotonic() - _snapshot["ts"] > _TTL:
        return ""
    return _snapshot["window"]


def clear() -> None:
    _snapshot["ts"] = 0.0
    _snapshot["elements"] = []
