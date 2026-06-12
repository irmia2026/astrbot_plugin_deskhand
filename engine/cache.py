"""
cache.py — 控件 ID ↔ RuntimeId 映射，保证多次 state() 调用间 ID 稳定。

UIA RuntimeId 是 int 元组，在控件所属进程生命周期内保持不变。
首次扫描时分配自增 id，上限 128 条（可通过配置覆盖）。
线程安全：所有可变操作受 threading.Lock 保护。
"""

import os
import json
import threading
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
    """id ↔ RuntimeId 双向映射缓存。同时存储 UIA 控件对象。线程安全。"""

    def __init__(self, max_size: int = MAX_CACHE_SIZE):
        self._id_to_rt: OrderedDict[int, tuple[int, ...]] = OrderedDict()
        self._rt_to_id: dict[tuple[int, ...], int] = {}
        self._id_to_control: dict[int, object] = {}  # UIA 控件对象
        self._next_id: int = 1
        self._max_size = max_size
        self._lock = threading.Lock()

    def get_id(self, runtime_id: tuple[int, ...]) -> int:
        """返回 RuntimeId 对应的稳定 id，不存在则分配新 id。"""
        with self._lock:
            if runtime_id in self._rt_to_id:
                return self._rt_to_id[runtime_id]
            cid = self._next_id
            self._next_id += 1
            self._id_to_rt[cid] = runtime_id
            self._rt_to_id[runtime_id] = cid
            return cid

    def get_runtime_id(self, cid: int) -> Optional[tuple[int, ...]]:
        """根据控件 id 查 RuntimeId，不存在返回 None。"""
        with self._lock:
            return self._id_to_rt.get(cid)

    def set_control(self, cid: int, control) -> None:
        """存储 UIA 控件对象。"""
        with self._lock:
            self._id_to_control[cid] = control

    def get_control(self, cid: int):
        """获取 UIA 控件对象，可能已过期。"""
        with self._lock:
            ctrl = self._id_to_control.get(cid)
            if ctrl is None:
                raise RuntimeError(f"控件 id={cid} 不在缓存中，请先调用 desk_state()")
        # 验证控件仍有效（在锁外执行，避免 UIA 调用阻塞其他线程）
        try:
            if not ctrl.Exists(0.1):
                self.remove(cid)
                raise RuntimeError(f"控件 id={cid} 已失效（控件已销毁）")
        except RuntimeError:
            raise
        except Exception:
            self.remove(cid)
            raise RuntimeError(f"控件 id={cid} 已失效")
        return ctrl

    def remove(self, cid: int) -> None:
        """手动移除一个条目（当控件不再存在时）。"""
        with self._lock:
            rt = self._id_to_rt.pop(cid, None)
            if rt is not None:
                self._rt_to_id.pop(rt, None)
            self._id_to_control.pop(cid, None)

    def clear(self) -> None:
        """清空缓存。"""
        with self._lock:
            self._id_to_rt.clear()
            self._rt_to_id.clear()
            self._id_to_control.clear()
            self._next_id = 1

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._id_to_rt)

    def __contains__(self, cid: int) -> bool:
        with self._lock:
            return cid in self._id_to_rt

    def all_ids(self) -> list[int]:
        """返回当前缓存中所有 id 的快照。"""
        with self._lock:
            return list(self._id_to_rt.keys())


# 全局单例（延迟初始化以支持配置）
_global_cache: Optional[ControlCache] = None
_global_lock = threading.Lock()


def get_global_cache() -> ControlCache:
    global _global_cache
    if _global_cache is None:
        with _global_lock:
            if _global_cache is None:
                _global_cache = ControlCache(max_size=_get_cache_size())
    return _global_cache
