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


# ── 多尺度参数 ────────────────────────────────────────────────
#
# 背景（真实反馈）：旧多尺度在真实全屏上从未生效过——
# 1) 触发条件是「词数<8 或 中位字高<12」，而真机 150% DPI 下界面文字高 16-20px，两条都不成立；
# 2) 就算触发，整图放大受 WinRT 边长上限（2600）限制：2560 宽全屏只能放大 1.0156×，
#    而代码里 `if scale <= 1.1: return items` 直接早退。
# 所以真正可行的做法是**分块放大**：每块放大后仍在上限内，再把结果合并回原图坐标。
_MAX_ENGINE_DIM = 2600   # WinRT OCR 最大边长（engine.max_image_dimension 的保守值）
_ENGINE_SAFE = 0.98      # 留安全边：正好卡在上限会让引擎内部再缩一次（双重重采样，实测 CER 反而变差）
_TILE_PX = 1200          # 分块边长（放大 2× = 2400 < 2600×0.98）
_TILE_OVERLAP = 96       # 块间重叠像素，避免文字被切在块边界
_TILE_SCALE = 2.0        # 分块放大倍数
_WHOLE_TRIGGER_WORDS = 8      # 触发下限：词数不足（稀疏画面）
_WHOLE_TRIGGER_H = 12         # 触发下限：中位字高过小
_TILE_TRIGGER_H = 16          # 分块放大触发：中位字高低于此值
#
# 阈值 16 的依据（两组真值实测，均在 150% DPI 本机）：
#   A) 合成真值（PIL 渲染已知文本，smoke_ocr_truth.py）：
#      10px 57%→21%、12px 57%→16%、14px 15%→10%、16px 14%→11%、20px 12%→12%
#   B) 真实渲染（记事本加载已知文件 + 缩放，probe_ocr_realzoom.py）：
#      12-13px 标题行 CER 33.3% → 16.7%（对半砍）；20px 19.0% → 19.0%（无收益）
#   C) 真实全屏（2560×1600，中位字高 17px）：分块与单遍基本平手，
#      但多花 1.35s（0.75s → 2.10s）
# 结论：多尺度只对**真正的小字号**划算（≥17px 的常规界面文字加放大没用——
# 那类错字是引擎对形近字的混淆，不是分辨率不够）。所以触发线取 16：
# 小字号屏幕/缩小的日志/长文本仍能吃到大幅提升，常规屏幕不白花时间。
# 需要“总是跑”可用 ocr_multiscale=force；关闭用 off。


def _median_h(words: list[dict]) -> float:
    hs = sorted(it["bottom"] - it["top"] for it in words)
    return hs[len(hs) // 2] if hs else 99.0


def _join_texts(texts: list[str]) -> str:
    """拼接词为行文本：中文直接相连，ASCII 词之间补空格（否则 HelloWorld）。"""
    out = ""
    for t in texts:
        if (out and out[-1].isascii() and out[-1].isalnum()
                and t[:1].isascii() and t[:1].isalnum()):
            out += " "
        out += t
    return out


def _rect_iou(a: dict, b: dict) -> float:
    ix = max(0, min(a["right"], b["right"]) - max(a["left"], b["left"]))
    iy = max(0, min(a["bottom"], b["bottom"]) - max(a["top"], b["top"]))
    inter = ix * iy
    if inter <= 0:
        return 0.0
    area_a = max(1, (a["right"] - a["left"]) * (a["bottom"] - a["top"]))
    area_b = max(1, (b["right"] - b["left"]) * (b["bottom"] - b["top"]))
    return inter / float(area_a + area_b - inter)


def _edge_dist(wd: dict) -> int:
    """词中心到所在块边界的距离（越大说明越靠近块中心，畸变越小、越可信）。"""
    box = wd.get("_box")
    if not box:
        return 1 << 20
    return min(wd["cx"] - box[0], box[2] - wd["cx"],
               wd["cy"] - box[1], box[3] - wd["cy"])


def _merge_tiled_words(words: list[dict]) -> list[dict]:
    """合并各块的词：重叠区的同一个词只留一个（离块中心更近的那份更可信）。"""
    kept: list[dict] = []
    for wd in sorted(words, key=_edge_dist, reverse=True):
        dup = False
        for k in kept:
            if (_rect_iou(wd, k) >= 0.4
                    or (abs(wd["cx"] - k["cx"]) <= 6 and abs(wd["cy"] - k["cy"]) <= 6)):
                dup = True
                break
        if not dup:
            kept.append(wd)
    for wd in kept:
        wd.pop("_box", None)
    return kept


def _rebuild_lines(words: list[dict]) -> list[dict]:
    """用词重建行级条目（分块会切断跨块的行，引擎原始行信息不可再用）。

    行级条目对中文整句匹配很重要（「弥亚之手」应该是一个整体，不是四个字）。
    同一行按垂直中心聚类；水平间隙过大的拆成多行（否则会把左右栏粘成一句）。
    """
    if not words:
        return []
    mh = max(6.0, _median_h(words))
    rows: list[dict] = []
    for wd in sorted(words, key=lambda x: (x["cy"], x["left"])):
        for row in rows:
            if abs(row["cy"] - wd["cy"]) < max(4.0, 0.6 * mh):
                row["items"].append(wd)
                row["cy"] = sum(i["cy"] for i in row["items"]) / len(row["items"])
                break
        else:
            rows.append({"cy": float(wd["cy"]), "items": [wd]})

    def _line_of(seg: list[dict]) -> dict:
        left = min(i["left"] for i in seg)
        top = min(i["top"] for i in seg)
        right = max(i["right"] for i in seg)
        bottom = max(i["bottom"] for i in seg)
        return {
            "text": _join_texts([i["text"] for i in seg]),
            "cx": (left + right) // 2, "cy": (top + bottom) // 2,
            "left": left, "top": top, "right": right, "bottom": bottom,
            "line": True,
        }

    lines: list[dict] = []
    for row in rows:
        its = sorted(row["items"], key=lambda x: x["left"])
        seg = [its[0]]
        for prev, cur in zip(its, its[1:]):
            if cur["left"] - prev["right"] > max(12.0, 1.5 * mh):
                lines.append(_line_of(seg))
                seg = [cur]
            else:
                seg.append(cur)
        lines.append(_line_of(seg))
    return lines


def _recognize_tiled(image, scale: float = _TILE_SCALE, tile: int = _TILE_PX,
                     overlap: int = _TILE_OVERLAP) -> list[dict]:
    """分块放大识别：每块单独放大后识别，再合并回原图坐标。

    分块的意义：整图放大撞 WinRT 边长上限（2560 宽只能放大 1.0156×），
    而每块（默认 1200px）放大 2× 后 2400px 仍在安全范围内。
    块大小会先按引擎上限夹一次：放大后超限时引擎会内部再缩一次，
    双重重采样反而把结果弄糟（基准实测整图放大 1.86× 时 10px 字 CER 95%）。
    """
    w, h = image.size
    engine = _detect_engine()
    if engine == "winrt":
        tile = min(tile, int(_MAX_ENGINE_DIM * _ENGINE_SAFE / max(1.0, scale)))
    tile = max(256, tile)
    step = max(64, tile - overlap)
    words: list[dict] = []
    for ty in range(0, h, step):
        for tx in range(0, w, step):
            box = (tx, ty, min(w, tx + tile), min(h, ty + tile))
            if box[2] - box[0] < 16 or box[3] - box[1] < 16:
                continue
            crop = image.crop(box)
            try:
                big = crop.resize(
                    (int(crop.size[0] * scale), int(crop.size[1] * scale)),
                    Image.Resampling.LANCZOS,
                )
            finally:
                crop.close()
            try:
                items = _recognize_once(big)
            finally:
                big.close()
            for it in items:
                if it.get("line"):
                    continue  # 行级条目改为合并后重建（跨块行会被切断）
                words.append({
                    "text": it["text"],
                    "left": box[0] + int(it["left"] / scale),
                    "top": box[1] + int(it["top"] / scale),
                    "right": box[0] + int(it["right"] / scale),
                    "bottom": box[1] + int(it["bottom"] / scale),
                    "cx": box[0] + int(it["cx"] / scale),
                    "cy": box[1] + int(it["cy"] / scale),
                    "_box": box,
                })
            if tx + tile >= w:
                break
        if ty + tile >= h:
            break
    merged = _merge_tiled_words(words)
    return merged + _rebuild_lines(merged)


# ── 统一入口 ────────────────────────────────────────────────────

def _recognize_once(image) -> list[dict]:
    engine = _detect_engine()
    if engine == "winrt":
        return _ocr_winrt(image)
    if engine == "rapidocr":
        return _ocr_rapid(image)
    return []


def recognize(image, min_words: int = _WHOLE_TRIGGER_WORDS,
              min_char_height: int = _WHOLE_TRIGGER_H,
              mode: str = "auto", stats: Optional[dict] = None) -> list[dict]:
    """识别 PIL 图像中的文字，返回 [{text, cx, cy, left, top, right, bottom}, ...]。

    多尺度策略（坐标永远保持原图空间，调用方零换算）：
    - 第一遍：原图直接识别；
    - auto（默认）：先试**整图放大**（词数不足或字高 <12px 时）；整图放大受引擎边长
      上限限制而不可行（scale ≤ 1.1）且中位字高 < _TILE_TRIGGER_H 时，改跑**分块放大**；
    - force：直接跑分块放大；off：只跑一遍。
    选用哪一遍：词数更多者胜（持平则取放大版——细节更清）。
    stats（可选，传入则写入诊断）：{"pass": ..., "scale": ..., "tiled": bool,
      "words_before": ..., "words_after": ..., "median_h_before": ..., "median_h_after": ...}
    """
    items = _recognize_once(image)
    words = [it for it in items if not it.get("line")]
    h_before = _median_h(words)
    if stats is not None:
        stats.update({"pass": "single", "tiled": False, "words_before": len(words),
                      "median_h_before": round(h_before, 1)})
    if mode == "off":
        return items

    w, h = image.size

    # ── 第二遍：分块放大 ──
    # 为什么不再用「整图放大」：真值基准实测（smoke_ocr_truth.py），整图放大在小字号上
    # 反而更差——10px 时 CER 56.8% → 95.1%，因为放大倍数顶到引擎边长上限，
    # 引擎内部又把它缩回去，等于双重重采样；而分块放大在同一组用例上全面胜出
    # （10px→21.0%、12px→16.0%、14px→9.9%、16px→11.1%）。
    # 图片很小时分块退化为单块，效果等同整图放大，所以统一走分块即可。
    needs_tiled = mode == "force" or (h_before < _TILE_TRIGGER_H) or len(words) < min_words
    if not needs_tiled or (w * h) < 200 * 200:
        return items
    try:
        items3 = _recognize_tiled(image)
    except Exception as e:
        # 带堆栈：分块失败会静默退回单遍，若吞掉细节就再也查不出原因
        # （真实教训：一次改名把 step 弄丢，NameError 被吞、多尺度静默失效）
        logger.warning("OCR 分块放大失败（回退单遍结果）: %s", e, exc_info=True)
        return items
    words3 = [it for it in items3 if not it.get("line")]
    if stats is not None:
        stats.update({"tiled": True, "scale": _TILE_SCALE,
                      "tile_px": _TILE_PX, "overlap": _TILE_OVERLAP,
                      "words_after": len(words3),
                      "median_h_after": round(_median_h(words3), 1)})
    # 词数更多者胜；持平取分块版（源分辨率更高）。词数明显变少则保留原结果（防退化）
    if len(words3) >= len(words):
        logger.info("OCR 分块放大命中: %d→%d 词 (scale=%.1f, tile=%d)",
                    len(words), len(words3), _TILE_SCALE, _TILE_PX)
        if stats is not None:
            stats["pass"] = "tiled"
        return items3
    logger.info("OCR 分块放大词数更少（%d < %d），保留单遍结果", len(words3), len(words))
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
