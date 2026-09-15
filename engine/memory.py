"""
memory.py — UI 元素记忆库（SQLite）。

记录每次成功定位的元素：在哪个应用（窗口类名）、目标描述、相对窗口坐标、
目标区域图像签名。下次同应用同目标直接用历史坐标 + 图像签名验证，
验证通过则跳过 OCR/VL 定位（0 次模型调用）。

坐标存「相对窗口」比例（0~1），窗口移动后仍然有效。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("deskhand.memory")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS element_memory (
    app TEXT NOT NULL,
    label TEXT NOT NULL,
    rel_x REAL NOT NULL,
    rel_y REAL NOT NULL,
    crop_sig TEXT,
    hits INTEGER NOT NULL DEFAULT 0,
    fails INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (app, label)
)
"""

# 图像签名允许的最大汉明距离（64 bit aHash；随机图案均值约 32，取 10 容忍渲染微差）
_MAX_HAMMING = 10
# 连续失败多少次后淘汰该记忆
_MAX_FAILS = 3


def normalize_label(label: str) -> str:
    # 去掉全部空白并小写："保存 按钮" 与 "保存按钮" 视为同一目标
    return "".join(str(label or "").lower().split())


def hamming(a: str, b: str) -> int:
    if not a or not b or len(a) != len(b):
        return 1 << 30
    return sum(bin(int(x, 16) ^ int(y, 16)).count("1") for x, y in zip(a, b))


class ElementMemory:
    def __init__(self, db_path: str):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._conn.execute(_SCHEMA)
            # 清理 class_name 时代的死数据（Chrome_WidgetWin_1 等）——
            # 现在 app 键是 exe 文件名，旧键永远不会再被命中
            self._conn.execute(
                "DELETE FROM element_memory WHERE app NOT LIKE '%.exe' AND app != 'unknown'"
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def lookup(self, app: str, label: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT rel_x, rel_y, crop_sig, hits, fails FROM element_memory "
                "WHERE app = ? AND label = ?",
                (app, normalize_label(label)),
            ).fetchone()
        if not row:
            return None
        return {
            "rel_x": row[0], "rel_y": row[1], "crop_sig": row[2],
            "hits": row[3], "fails": row[4],
        }

    def upsert(self, app: str, label: str, rel_x: float, rel_y: float,
               crop_sig: str, success: bool) -> None:
        """成功：hits+1、fails 清零、更新坐标与签名；失败：只 fails+1，
        不动 hits，也不用失败位置的坐标/签名覆盖旧记忆；连续失败自动淘汰。"""
        now = datetime.now(timezone.utc).isoformat()
        label_n = normalize_label(label)
        with self._lock:
            row = self._conn.execute(
                "SELECT rel_x, rel_y, crop_sig, hits, fails FROM element_memory "
                "WHERE app = ? AND label = ?",
                (app, label_n),
            ).fetchone()
            if row:
                if success:
                    new_rel_x, new_rel_y, new_sig = rel_x, rel_y, crop_sig
                    hits, fails = row[3] + 1, 0
                else:
                    new_rel_x, new_rel_y, new_sig = row[0], row[1], row[2]
                    hits, fails = row[3], row[4] + 1
            else:
                new_rel_x, new_rel_y, new_sig = rel_x, rel_y, crop_sig
                hits, fails = (1, 0) if success else (0, 1)
            self._conn.execute(
                """
                INSERT INTO element_memory (app, label, rel_x, rel_y, crop_sig, hits, fails, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(app, label) DO UPDATE SET
                    rel_x = excluded.rel_x, rel_y = excluded.rel_y,
                    crop_sig = excluded.crop_sig, hits = excluded.hits,
                    fails = excluded.fails, updated_at = excluded.updated_at
                """,
                (app, label_n, new_rel_x, new_rel_y, new_sig, hits, fails, now),
            )
            self._conn.execute(
                "DELETE FROM element_memory WHERE fails >= ?", (_MAX_FAILS,)
            )
            self._conn.commit()

    def validate_sig(self, stored_sig: str, current_sig: str) -> bool:
        """图像签名汉明距离在阈值内视为同一目标。"""
        return hamming(stored_sig, current_sig) <= _MAX_HAMMING


# ── 图像签名（aHash，供记忆验证用）───────────────────────────────

def image_signature(image) -> str:
    """64-bit aHash：缩到 8x8 灰度，与均值比较。对轻微渲染变化鲁棒。"""
    img = image.convert("L").resize((8, 8))
    px = list(img.getdata())
    avg = sum(px) / len(px)
    bits = 0
    for p in px:
        bits = (bits << 1) | (1 if p >= avg else 0)
    return f"{bits:016x}"


def crop_signature(image, cx: int, cy: int, size: int = 64) -> str:
    """取 (cx, cy) 周围 size×size 区域的签名。"""
    half = size // 2
    w, h = image.size
    box = (
        max(0, cx - half), max(0, cy - half),
        min(w, cx + half), min(h, cy + half),
    )
    if box[2] <= box[0] or box[3] <= box[1]:
        return ""
    return image_signature(image.crop(box))
