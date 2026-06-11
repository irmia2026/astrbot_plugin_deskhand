"""
cache.py — 控件 ID ↔ RuntimeId 映射，保证多次 state() 调用间 ID 稳定。

UIA RuntimeId 是 int 元组，在控件所属进程生命周期内保持不变。
首次扫描时分配自增 id，LRU 淘汰，上限 128 条。
"""

from collections import OrderedDict
from typing import Optional

MAX_CACHE_SIZE = 128


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
            oldest, _ = self._id_to_rt.popitem(last=False)
            del self._rt_to_id[oldest]
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


# 全局单例
_global_cache = ControlCache()


def get_global_cache() -> ControlCache:
    return _global_cache
