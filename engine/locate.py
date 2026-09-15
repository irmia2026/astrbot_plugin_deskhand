"""
locate.py — 三级定位引擎：记忆库 → OCR → VL 漏斗。

核心原则：能用确定性手段拿到坐标的，绝不让 VL 输出坐标。
- L1 记忆库：历史成功坐标 + 局部图像签名验证（0 次模型调用）；
- L2 OCR：本地 OCR 找文字目标的精确 bbox（0 次模型调用）；
- L3 VL 漏斗：VL 只做粗定位（3×3 格子），工程层裁剪放大后让 VL 在
  小区域里指像素点，最后换算回屏幕坐标（1-2 次模型调用，且每次 ≤384 token）。

所有截图/OCR 在 desktop.run() 线程执行；VL 调用为 httpx 异步。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from . import desktop, memory as mem, ocr, vl

logger = logging.getLogger("deskhand.locate")

# hover-verify 时画在截图上的标记半径
_MARKER_R = 14


class LocateError(RuntimeError):
    pass


# ── 网格标注 ────────────────────────────────────────────────────

_CELL_COLS = "ABCDEFGHI"  # 3x3 时用 A-C


def _draw_grid(img, cols: int = 3, rows: int = 3):
    """在图像副本上画等分网格 + 格子编号（A1..C3），返回 (标注图, 格子rect映射)。"""
    from PIL import ImageDraw, ImageFont

    out = img.copy()
    draw = ImageDraw.Draw(out)
    w, h = out.size
    cw, ch = w / cols, h / rows
    try:
        font = ImageFont.truetype("arial.ttf", max(18, int(min(cw, ch) * 0.12)))
    except Exception:
        font = ImageFont.load_default()

    cells = {}
    for r in range(rows):
        for c in range(cols):
            label = f"{_CELL_COLS[c]}{r + 1}"
            rect = (int(c * cw), int(r * ch), int((c + 1) * cw), int((r + 1) * ch))
            cells[label] = rect
            draw.rectangle(rect, outline="#FF3333", width=3)
            # 格子编号放左上角，带底色便于 VL 辨认
            tx, ty = rect[0] + 8, rect[1] + 6
            try:
                tb = draw.textbbox((tx, ty), label, font=font)
                draw.rectangle(tb, fill="#FF3333")
            except Exception:
                pass
            draw.text((tx, ty), label, fill="white", font=font)
    return out, cells


# 元素标注配色（按来源）
_SOURCE_COLORS = {
    "ocr": "#00C853",   # 绿：OCR 文字（精确）
    "vl": "#FF9800",    # 橙：VL 识别（近似）
    "cv": "#2196F3",    # 蓝：CV 候选框（无语义）
    "memory": "#9C27B0"  # 紫：记忆库
}


def annotate_elements(img, numbered: list[dict]):
    """在图像副本上画出注册元素并标号（e1..eN）。

    有 left/top/right/bottom 的画真实边框；只有中心点的画固定小方框。
    标签为元素 id（与 elements_card 一一对应），带底色保证可读。
    """
    from PIL import ImageDraw, ImageFont

    out = img.copy()
    draw = ImageDraw.Draw(out)
    w, h = out.size
    try:
        font = ImageFont.truetype("arial.ttf", max(14, w // 110))
    except Exception:
        font = ImageFont.load_default()

    for el in numbered:
        color = _SOURCE_COLORS.get(el.get("source", ""), "#FF2222")
        if all(el.get(k) is not None for k in ("left", "top", "right", "bottom")):
            box = (el["left"], el["top"], el["right"], el["bottom"])
        else:
            # 只有中心点（VL 元素）：画固定小方框
            r = 24
            box = (el["x"] - r, el["y"] - r, el["x"] + r, el["y"] + r)
        draw.rectangle(box, outline=color, width=3)
        label = el["id"]
        tx, ty = box[0], max(0, box[1] - font.size - 6)
        try:
            tb = draw.textbbox((tx, ty), label, font=font)
            draw.rectangle((tb[0] - 2, tb[1] - 2, tb[2] + 4, tb[3] + 3), fill=color)
        except Exception:
            pass
        draw.text((tx, ty), label, fill="white", font=font)
    return out


def draw_marker(img, x: int, y: int, color: str = "#FF2222"):
    """在图像副本的 (x, y) 处画十字准星标记（hover-verify 用，表示「将要点击这里」）。"""
    from PIL import ImageDraw

    out = img.copy()
    draw = ImageDraw.Draw(out)
    r = _MARKER_R
    draw.ellipse((x - r, y - r, x + r, y + r), outline=color, width=4)
    draw.line((x - r - 6, y, x + r + 6, y), fill=color, width=3)
    draw.line((x, y - r - 6, x, y + r + 6), fill=color, width=3)
    return out


# ── VL 回答解析 ─────────────────────────────────────────────────

def parse_cell(text: str, cols: int = 3, rows: int = 3) -> Optional[str]:
    # 注意不能用 \b：CJK 字符在 Python re 中是 word 字符，"在B2" 中 B 前没有边界
    m = re.search(r"(?<![A-Z0-9])([A-Z])\s*([0-9])(?![0-9])", text.upper())
    if not m:
        return None
    label = f"{m.group(1)}{m.group(2)}"
    col = _CELL_COLS.find(m.group(1))
    row = int(m.group(2)) - 1
    if 0 <= col < cols and 0 <= row < rows:
        return label
    return None


def parse_xy(text: str) -> Optional[tuple[int, int]]:
    # 取回答中的前两个数字："x=325，y=418" / "(325, 418)" / {"dx": 12, "dy": -5} 均可
    nums = re.findall(r"-?\d+", text)
    if len(nums) >= 2:
        return int(nums[0]), int(nums[1])
    return None


def parse_yes_no(text: str) -> Optional[bool]:
    t = text.strip().lower()
    # 先剥掉「是否」——否则否定分支的「否」会把中性表述恒判为 False
    t = t.replace("是否", "")
    # 先查否定（"不在目标上" 包含 "在目标上"，顺序不能反）
    if re.search(r"\bno\b|不是|不对|不在|未在|没有|没压|否\s*$|否[，。,.：:]", t):
        return False
    if re.search(r"\byes\b|正确|在目标上|压在|是\s*$|是[，。,.：:]", t):
        return True
    return None


# ── 定位主流程 ──────────────────────────────────────────────────

async def locate(target: str, *, window_kw: str = "", use_memory: bool = True,
                 use_ocr: bool = True, max_zoom: int = 2) -> dict:
    """定位目标，返回 {x, y, source, win, rel}。失败抛 LocateError。

    source: "memory" | "ocr" | "vl_funnel"
    """
    target = (target or "").strip()
    if not target:
        raise LocateError("target 不能为空")

    # 确定作用窗口
    win = None
    if window_kw:
        win = await desktop.run(desktop.find_window, window_kw)
        if win is None:
            raise LocateError(f"找不到窗口「{window_kw}」")
        if not desktop.valid_rect(win.get("rect")):
            raise LocateError(
                f"窗口「{win['title']}」已最小化或不可见，请先 window_action(restore)"
            )
    else:
        win = await desktop.run(desktop.foreground_window)
    bbox = win["rect"] if win and desktop.valid_rect(win.get("rect")) else None
    app_key = desktop.app_key(win) if win else "unknown"

    # 当前截图（L1 验证 / L2 OCR / L3 漏斗都基于同一帧，保证一致性）
    shot = await desktop.run(desktop.screenshot, bbox)
    # 全屏截图的原点是虚拟屏原点（多显示器时可能为负），不是 (0,0)
    origin_x, origin_y = (
        (bbox[0], bbox[1]) if bbox else await desktop.run(desktop.virtual_screen_origin)
    )
    win_w, win_h = shot.size

    # L1 记忆库
    if use_memory and _memory is not None and bbox:
        rec = _memory.lookup(app_key, target)
        if rec:
            cx = int(origin_x + rec["rel_x"] * win_w)
            cy = int(origin_y + rec["rel_y"] * win_h)
            sig = mem.crop_signature(shot, cx - origin_x, cy - origin_y)
            if rec["crop_sig"] and _memory.validate_sig(rec["crop_sig"], sig):
                logger.info("L1 记忆命中: %s @ (%d,%d)", target, cx, cy)
                return {"x": cx, "y": cy, "source": "memory", "win": win,
                        "shot": shot, "hits": rec["hits"]}
            logger.info("L1 记忆签名不匹配，降级: %s", target)

    # L2 OCR（文字目标）
    if use_ocr and ocr.available():
        try:
            items = await desktop.run(ocr.recognize, shot)
            hit = ocr.find_text(items, target)
            if hit:
                cx = origin_x + hit["cx"]
                cy = origin_y + hit["cy"]
                logger.info("L2 OCR 命中: %s @ (%d,%d)", target, cx, cy)
                return {"x": cx, "y": cy, "source": "ocr", "win": win, "shot": shot}
        except Exception as e:
            logger.warning("OCR 识别失败，降级 VL 漏斗: %s", e)

    # L3 VL 漏斗
    if not vl.vl_available():
        raise LocateError(
            f"无法定位「{target}」：记忆未命中，OCR 未找到文字，且未配置 VL 模型"
        )
    point = await _vl_funnel(shot, target, max_zoom=max_zoom)
    if point is None:
        shot.close()
        raise LocateError(f"VL 无法定位「{target}」")
    logger.info("L3 VL 漏斗命中: %s @ %s", target, point)
    return {"x": origin_x + point[0], "y": origin_y + point[1],
            "source": "vl_funnel", "win": win, "shot": shot}


async def _vl_funnel(shot, target: str, max_zoom: int = 2) -> Optional[tuple[int, int]]:
    """VL 漏斗：3×3 格子粗定位 → 裁剪放大 → 像素点精定位。返回图像内坐标。"""
    img = shot
    off_x, off_y = 0, 0
    for level in range(max_zoom):
        w, h = img.size
        if w < 220 or h < 160:
            break  # 区域已足够小，直接指点
        gridded, cells = _draw_grid(img)
        try:
            ans = await vl.ask(
                gridded,
                f"这张界面截图被红色网格划分为若干格子。目标「{target}」最可能在哪个格子？"
                f"只回答格子编号（如 A1、B2），不要解释。",
                max_tokens=1024,  # 推理模型的思维链会占配额，不能给太小
            )
        finally:
            gridded.close()
        cell = parse_cell(ans)
        if not cell:
            # 模型没按格式回答：尝试直接解析坐标——但要做合理性校验，
            # 「第2行第3列」这类回答里的数字不是像素坐标
            xy = parse_xy(ans)
            if xy and xy[0] > 20 and xy[1] > 20 and xy[0] < w and xy[1] < h:
                return _map_model_point(xy, img.size, (off_x, off_y))
            return None
        rect = cells[cell]
        # 裁剪命中格子（带 15% 外扩，防止目标压在格线上）
        pad_x = int((rect[2] - rect[0]) * 0.15)
        pad_y = int((rect[3] - rect[1]) * 0.15)
        crop_box = (
            max(0, rect[0] - pad_x), max(0, rect[1] - pad_y),
            min(w, rect[2] + pad_x), min(h, rect[3] + pad_y),
        )
        new_img = img.crop(crop_box)
        off_x += crop_box[0]
        off_y += crop_box[1]
        if img is not shot:
            img.close()
        img = new_img

    # 最终区域直接指点
    marked = img  # 不再画网格，直接问坐标
    ans = await vl.ask(
        marked,
        f"这是一张界面局部截图。目标「{target}」的中心在图中的哪个像素位置？"
        f"以图片左上角为原点 (0,0)，只回答坐标，格式: x,y",
        max_tokens=1024,
    )
    xy = parse_xy(ans)
    if img is not shot:
        img.close()
    if not xy:
        return None
    return _map_model_point(xy, img.size, (off_x, off_y))


def _map_model_point(xy: tuple[int, int], img_size: tuple[int, int],
                     offset: tuple[int, int]) -> tuple[int, int]:
    """VL 看到的是被预缩放到长边 ≤768 的图；把它的回答换算回原图坐标，再加偏移。
    结果钳制在虚拟屏范围内（VL 幻觉坐标不外溢）。"""
    w, h = img_size
    scale = min(1.0, vl.VL_IMAGE_LONG_EDGE / max(w, h))
    mx, my = xy
    if scale < 1.0:
        mx = mx / scale
        my = my / scale
    x = int(offset[0] + mx)
    y = int(offset[1] + my)
    left, top, right, bottom = desktop.screen_bounds()
    return (max(left, min(x, right - 1)), max(top, min(y, bottom - 1)))


# ── hover-verify ────────────────────────────────────────────────

async def verify_point(x: int, y: int, target: str, bbox: Optional[tuple] = None) -> dict:
    """在 (x,y) 画标记并截图，让 VL 确认标记是否压在目标上。
    返回 {ok, dx, dy}——ok=False 时 dx/dy 为 VL 建议的修正方向（像素，可能为 None）。"""
    shot = await desktop.run(desktop.screenshot, bbox)
    # 裁剪目标周围区域（省 token 且更聚焦）；全屏时原点是虚拟屏原点（多屏可能为负）
    origin_x, origin_y = (
        (bbox[0], bbox[1]) if bbox else await desktop.run(desktop.virtual_screen_origin)
    )
    half = 160
    w, h = shot.size
    box = (max(0, x - origin_x - half), max(0, y - origin_y - half),
           min(w, x - origin_x + half), min(h, y - origin_y + half))
    crop = shot.crop(box)
    marked = draw_marker(crop, x - origin_x - box[0], y - origin_y - box[1])
    shot.close()
    crop.close()
    try:
        ans = await vl.ask(
            marked,
            f"截图中有一个红色十字准星标记。请判断：准星中心是否精确压在「{target}」上？\n"
            f"只回答 JSON：{{\"on_target\": true/false, \"dx\": 整数, \"dy\": 整数}}\n"
            f"dx/dy 为目标中心相对准星中心的像素偏移（目标在准星右侧 dx 为正，下方 dy 为正）。",
            max_tokens=1024,
            json_mode=True,
        )
    finally:
        marked.close()
    import json as _json

    m = re.search(r"\{[^{}]*\}", ans, re.S)
    if m:
        try:
            data = _json.loads(m.group(0))
            return {
                "ok": bool(data.get("on_target")),
                "dx": int(data.get("dx") or 0),
                "dy": int(data.get("dy") or 0),
            }
        except Exception:
            pass
    yn = parse_yes_no(ans)
    return {"ok": bool(yn), "dx": 0, "dy": 0}


# ── 记忆库注入 ──────────────────────────────────────────────────

_memory: Optional[mem.ElementMemory] = None


def set_memory(store: Optional[mem.ElementMemory]) -> None:
    global _memory
    _memory = store


def get_memory() -> Optional[mem.ElementMemory]:
    return _memory
