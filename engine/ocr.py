"""
ocr.py — 本地 OCR 引擎（可插拔，零硬依赖）。

优先级：
1. WinRT OCR（Windows 10+ 系统自带，需 `pip install winsdk`）——离线、免费、中文好；
2. RapidOCR（需 `pip install rapidocr-onnxruntime`）——跨平台本地模型；
3. 两者都不可用 → available() 返回 False，定位引擎自动降级到 VL 漏斗。

所有识别函数为同步阻塞，约定在 desktop.run() 的专用线程中调用。
"""

from __future__ import annotations

import io
import logging
from typing import Optional

from PIL import Image

logger = logging.getLogger("deskhand.ocr")

_engine_cache: Optional[str] = None  # "winrt" | "rapidocr" | "none"


def _detect_engine() -> str:
    global _engine_cache
    if _engine_cache is not None:
        return _engine_cache
    try:
        import winsdk  # noqa: F401

        _engine_cache = "winrt"
        return _engine_cache
    except ImportError:
        pass
    try:
        from rapidocr_onnxruntime import RapidOCR  # noqa: F401

        _engine_cache = "rapidocr"
        return _engine_cache
    except ImportError:
        pass
    _engine_cache = "none"
    logger.info("未检测到本地 OCR 引擎（winsdk / rapidocr-onnxruntime），OCR 定位通道关闭")
    return _engine_cache


def available() -> bool:
    return _detect_engine() != "none"


# ── WinRT OCR ───────────────────────────────────────────────────

def _ocr_winrt(image) -> list[dict]:
    """用 Windows.Media.Ocr 识别 PIL 图像，返回词级+行级 [{text, cx, cy, left, top, right, bottom}]。

    坐标一律换算回原图像素系（内部可能因 WinRT 边长限制先压缩再放大）。
    """
    import asyncio

    from winsdk.windows.graphics.imaging import BitmapDecoder
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.storage.streams import DataWriter, InMemoryRandomAccessStream

    async def _decode(data: bytes):
        stream = InMemoryRandomAccessStream()
        writer = DataWriter(stream)
        writer.write_bytes(data)
        await writer.store_async()
        writer.detach_stream()  # 防止 writer.close() 带走底层流
        writer.close()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        return await decoder.get_software_bitmap_async()

    async def _run() -> list[dict]:
        engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            from winsdk.windows.globalization import Language

            engine = OcrEngine.try_create_from_language(Language("zh-Hans-CN"))
        if engine is None:
            raise RuntimeError("WinRT OcrEngine 创建失败")

        # WinRT OCR 有最大边长限制（通常 2600px），超出先用 PIL 压缩
        max_dim = int(getattr(engine, "max_image_dimension", 2600))
        src = image.convert("RGB")
        scale_back = 1.0
        if max(src.size) > max_dim:
            scale_back = max(src.size) / max_dim
            src = src.copy()
            src.thumbnail((max_dim, max_dim))

        buf = io.BytesIO()
        src.save(buf, "PNG")
        sbmp = await _decode(buf.getvalue())

        result = await engine.recognize_async(sbmp)
        items: list[dict] = []

        def _scaled(r):
            left = int(r.x * scale_back)
            top = int(r.y * scale_back)
            right = int((r.x + r.width) * scale_back)
            bottom = int((r.y + r.height) * scale_back)
            return left, top, right, bottom

        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            for w in words:
                left, top, right, bottom = _scaled(w.bounding_rect)
                items.append(
                    {
                        "text": w.text,
                        "cx": (left + right) // 2,
                        "cy": (top + bottom) // 2,
                        "left": left, "top": top, "right": right, "bottom": bottom,
                    }
                )
            # 行级条目（整行文本 + 外接框），便于匹配被拆成多个词的目标
            left = min(_scaled(w.bounding_rect)[0] for w in words)
            top = min(_scaled(w.bounding_rect)[1] for w in words)
            right = max(_scaled(w.bounding_rect)[2] for w in words)
            bottom = max(_scaled(w.bounding_rect)[3] for w in words)
            items.append(
                {
                    "text": "".join(w.text for w in words),
                    "cx": (left + right) // 2,
                    "cy": (top + bottom) // 2,
                    "left": left, "top": top, "right": right, "bottom": bottom,
                    "line": True,
                }
            )
        return items

    return asyncio.run(_run())


# ── RapidOCR ────────────────────────────────────────────────────

_rapid_engine = None


def _ocr_rapid(image) -> list[dict]:
    global _rapid_engine
    from rapidocr_onnxruntime import RapidOCR
    import numpy as np

    if _rapid_engine is None:
        _rapid_engine = RapidOCR()
    arr = np.array(image.convert("RGB"))
    result, _ = _rapid_engine(arr)
    items: list[dict] = []
    for entry in result or []:
        pts, text = entry[0], entry[1]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        left, right = int(min(xs)), int(max(xs))
        top, bottom = int(min(ys)), int(max(ys))
        items.append(
            {
                "text": text,
                "cx": (left + right) // 2,
                "cy": (top + bottom) // 2,
                "left": left, "top": top, "right": right, "bottom": bottom,
            }
        )
    return items


# ── 统一入口 ────────────────────────────────────────────────────

def _recognize_once(image) -> list[dict]:
    engine = _detect_engine()
    if engine == "winrt":
        return _ocr_winrt(image)
    if engine == "rapidocr":
        return _ocr_rapid(image)
    return []


def recognize(image, min_words: int = 8, min_char_height: int = 12) -> list[dict]:
    """识别 PIL 图像中的文字，返回 [{text, cx, cy, left, top, right, bottom}, ...]。

    多尺度重试（坐标永远保持原图空间，调用方零换算）。双触发条件：
    - 整图词数 < min_words（稀疏画面）；
    - 词的中位字高 < min_char_height（小字号——真实屏幕词数再多也会触发）。
    满足任一就放大重跑，取词数更多（或持平但更清晰）的一遍，坐标折回原图。
    """
    items = _recognize_once(image)
    words = [it for it in items if not it.get("line")]

    def _median_h(ws: list[dict]) -> float:
        hs = sorted(it["bottom"] - it["top"] for it in ws)
        return hs[len(hs) // 2] if hs else 99

    if len(words) >= min_words and _median_h(words) >= min_char_height:
        return items

    w, h = image.size
    scale = 2.0
    # WinRT 有最大边长限制，放大不得超过；RapidOCR 无此限制
    if _detect_engine() == "winrt":
        scale = min(scale, 2600 / max(w, h))
    if scale <= 1.1:
        return items

    try:
        big = image.resize((int(w * scale), int(h * scale)), Image.Resampling.LANCZOS)
        items2 = _recognize_once(big)
        big.close()
    except Exception:
        return items

    words2 = [it for it in items2 if not it.get("line")]
    # 词数更多、或持平但字高更优（更清晰）时采用放大结果
    if len(words2) > len(words) or (
        len(words2) == len(words) and _median_h(words2) > _median_h(words)
    ):
        inv = 1.0 / scale
        for it in items2:
            for k in ("cx", "cy", "left", "top", "right", "bottom"):
                it[k] = int(it[k] * inv)
        logger.info("OCR 多尺度命中: %d→%d 词, 中位字高 %.0f→%.0f (scale=%.2f)",
                    len(words), len(words2), _median_h(words), _median_h(words2), scale)
        return items2
    return items


def items_to_elements(items: list[dict], origin_x: int = 0, origin_y: int = 0) -> list[dict]:
    """把 OCR items 转成元素卡片用的 element 列表（含屏幕坐标与 bbox）。

    行级条目优先（WinRT 构造的整行能保住中文整句——「弥亚之手」是一个元素，
    而不是「弥」「亚」「之」「手」四个）；不被任何行覆盖的词级条目作补充。
    """
    lines = [it for it in items if it.get("line")]
    words = [it for it in items if not it.get("line")]

    def to_el(it) -> dict:
        return {
            "name": it["text"], "type": "text",
            "x": origin_x + it["cx"], "y": origin_y + it["cy"],
            "left": origin_x + it["left"], "top": origin_y + it["top"],
            "right": origin_x + it["right"], "bottom": origin_y + it["bottom"],
            "has_icon": False, "source": "ocr",
        }

    if not lines:
        return [to_el(it) for it in words]
    elements = [to_el(it) for it in lines]
    for w in words:
        cx, cy = w["cx"], w["cy"]
        covered = any(
            ln["left"] <= cx <= ln["right"] and ln["top"] <= cy <= ln["bottom"]
            for ln in lines
        )
        if not covered:
            elements.append(to_el(w))
    return elements


def find_text(items: list[dict], target: str) -> Optional[dict]:
    """在 OCR 结果中查找目标文字（忽略大小写与空白）。

    匹配策略（修复同行多按钮误点）：
    1. 词级精确匹配（规范化后相等）——「确定」命中词「确定」而不是整行「取消确定」；
    2. 行级精确匹配——目标被拆成多词时整行就是答案；
    3. 词级包含（长度最接近者优先）；
    4. 行级包含（长度最接近者优先）。
    """
    t = "".join(target.split()).lower()
    if not t:
        return None

    def norm(s) -> str:
        return "".join(str(s).split()).lower()

    lines = [it for it in items if it.get("line")]
    words = [it for it in items if not it.get("line")]

    def exact(pool):
        return [it for it in pool if norm(it["text"]) == t]

    def contains(pool):
        hits = [it for it in pool if t in norm(it["text"])]
        hits.sort(key=lambda it: abs(len(norm(it["text"])) - len(t)))
        return hits

    for pool in (words, lines):
        hits = exact(pool)
        if hits:
            return hits[0]
    for pool in (words, lines):
        hits = contains(pool)
        if hits:
            return hits[0]
    return None
