"""
cache.py — 控件 ID ↔ RuntimeId 映射，保证多次 state() 调用间 ID 稳定。

UIA RuntimeId 是 int 元组，在控件所属进程生命周期内保持不变。
首次扫描时分配自增 id，LRU 淘汰，上限 128 条（可通过配置覆盖）。
"""

import os
import json
from collections import OrderedDict
from typing import Optional

MAX_CACHE_SIZE = 128


def _get_cache_size() -> int:
    """尝试从配置读取 cache_size，失败返回默认值。"""
    try:
        config_path = os.environ.get("ASTRBOT_CONFIG_PATH", "")
        if config_path and os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            plugin_cfg = cfg.get("astrbot_plugin_deskhand", {})
            return plugin_cfg.get("cache_size", MAX_CACHE_SIZE)
    except Exception:
        pass
    return MAX_CACHE_SIZE


class ControlCache:
    """id ↔ RuntimeId 双向映射缓存，LRU 淘汰。"""

    def __init__(self, max_size: int = MAX_CACHE_SIZE):
        self._id_to_rt: OrderedDict[int, tuple[int, ...]] = OrderedDict()
        self._rt_to_id: dict[tuple[int, ...], int] = {}
        self._next_id: int = 1
        self._max_size = max_size

    def get_id(self, runtime_id: tuple[int, ...]) -> int:
        """返回 RuntimeId 对应的稳定 id，不存在则分配新 id。"""
        if runtime_id in self._rt_to_id:
            cid = self._rt_to_id[runtime_id]
            # LRU touch
            self._id_to_rt.move_to_end(cid)
            return cid
        # 淘汰最久未用
        if len(self._id_to_rt) >= self._max_size:
            oldest_cid, oldest_rt = self._id_to_rt.popitem(last=False)
            del self._rt_to_id[oldest_rt]
        cid = self._next_id
        self._next_id += 1
        self._id_to_rt[cid] = runtime_id
        self._rt_to_id[runtime_id] = cid
        return cid

    def get_runtime_id(self, cid: int) -> Optional[tuple[int, ...]]:
        """根据控件 id 查 RuntimeId，不存在返回 None。"""
        return self._id_to_rt.get(cid)

    def remove(self, cid: int) -> None:
        """手动移除一个条目（当控件不再存在时）。"""
        rt = self._id_to_rt.pop(cid, None)
        if rt is not None:
            self._rt_to_id.pop(rt, None)

    def clear(self) -> None:
        """清空缓存。"""
        self._id_to_rt.clear()
        self._rt_to_id.clear()
        self._next_id = 1

    @property
    def size(self) -> int:
        return len(self._id_to_rt)

    def __contains__(self, cid: int) -> bool:
        return cid in self._id_to_rt

    def all_ids(self) -> list[int]:
        """返回当前缓存中所有 id 的快照。"""
        return list(self._id_to_rt.keys())


# 全局单例（延迟初始化以支持配置）
_global_cache: Optional[ControlCache] = None


def get_global_cache() -> ControlCache:
    global _global_cache
    if _global_cache is None:
        _global_cache = ControlCache(max_size=_get_cache_size())
    return _global_cache
