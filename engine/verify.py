"""
verify.py — 图像 diff 验证器（PIL ImageChops，纯 C 速度）。

取代 v1 中纯 Python 逐像素循环（全屏 200 万像素要数秒）的实现。
用途：
- 动作前后对比，给出确定性的「画面是否变化 / 变化在哪」信号；
- wait_for_change：轮询等待画面变化，替代盲 sleep。
"""

from __future__ import annotations

from typing import Optional

from PIL import ImageChops

# 灰度差超过该值视为变化像素
_DIFF_THRESHOLD = 24


def diff_images(img_before, img_after, threshold: int = _DIFF_THRESHOLD) -> dict:
    """对比两张同尺寸图像，返回 {changed, percent, bbox}。尺寸不同直接 changed=True。"""
    if img_before.size != img_after.size:
        return {"changed": True, "percent": None, "bbox": None, "size_changed": True}

    diff = ImageChops.difference(img_before.convert("L"), img_after.convert("L"))
    # 阈值化：<= threshold 归零，其余拉到 255
    bw = diff.point(lambda p: 255 if p > threshold else 0)
    bbox = bw.getbbox()
    if bbox is None:
        return {"changed": False, "percent": 0.0, "bbox": None, "size_changed": False}

    w, h = img_before.size
    # 阈值化后非零像素都集中在灰度 255
    hist = bw.histogram()
    changed_px = hist[255] if len(hist) > 255 else sum(hist[1:])
    percent = round(changed_px / (w * h) * 100, 2)
    return {"changed": True, "percent": percent, "bbox": list(bbox), "size_changed": False}


def wait_for_change(region: Optional[list] = None, timeout: float = 5.0,
                    interval: float = 0.25, reference=None) -> dict:
    """轮询截图直到画面变化或超时。region=[x, y, w, h]；reference 为参考图（默认取当前画面）。

    同步阻塞，约定在 desktop.run() 中调用。
    """
    import time

    from . import desktop

    bbox = None
    if region and len(region) == 4:
        x, y, w, h = [int(v) for v in region]
        bbox = (x, y, x + w, y + h)

    ref = reference if reference is not None else desktop.screenshot(bbox)
    deadline = time.monotonic() + max(0.1, timeout)
    while time.monotonic() < deadline:
        time.sleep(interval)
        cur = desktop.screenshot(bbox)
        d = diff_images(ref, cur)
        cur.close()
        if d["changed"]:
            return {"changed": True, "elapsed": round(timeout - max(0.0, deadline - time.monotonic()), 2),
                    "percent": d["percent"], "bbox": d["bbox"]}
    return {"changed": False, "timeout": timeout}
