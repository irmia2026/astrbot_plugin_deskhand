"""
vl.py — VL 模型访问层。

provider 降级链解析顺序：
1. 复用同时安装的 irmia_vision 插件（已在 AstrBot 中加载时，直接取其已解析的降级链；
   未加载但文件存在时导入其 config 模块尝试解析）；
2. 本插件配置 vl_provider_1/2/3（下拉）/ vl_provider_ids（手动）→ 匹配 AstrBot 已保存模型；
3. 本插件配置 vl_model（手动 base_url/api_key/model）。

VL 调用内置实现（httpx 异步，OpenAI 兼容 chat/completions）：
- 图片统一 JPEG 压缩、长边 ≤768（DeepSeek 视觉模型会缩放到 ~800x800 等效像素，
  预缩放使「模型看到的坐标系」与本地坐标系的换算变得确定）；
- content 为空时回退 reasoning_content（DeepSeek 推理型视觉模型）。
"""

from __future__ import annotations

import base64
import importlib
import io
import logging
import sys
from typing import Optional

import httpx

logger = logging.getLogger("deskhand.vl")

# 发给 VL 的图片长边上留 768，保证模型侧缩放后坐标可确定换算
VL_IMAGE_LONG_EDGE = 768

_config: dict = {}
_context = None  # AstrBot Context


def setup(config: dict, context) -> None:
    global _config, _context
    _config = config or {}
    _context = context


# ── provider 降级链 ─────────────────────────────────────────────

def _chain_from_irmia_vision() -> Optional[list[dict]]:
    """复用同安装的 irmia_vision 插件的 VL 降级链。"""
    # 1) 已在 sys.modules（插件已加载，模块对象上已有 providers）
    for key, mod in list(sys.modules.items()):
        if key.endswith("irmia_vision.tools.config") and hasattr(mod, "resolve_provider_chain"):
            try:
                chain = mod.resolve_provider_chain()
                if chain:
                    logger.info("复用 irmia_vision 的 VL 降级链（%d 个模型）", len(chain))
                    return chain
            except Exception as e:
                logger.warning("读取 irmia_vision 降级链失败: %s", e)
    # 2) 文件存在但未加载：尝试按标准插件路径导入（此时其 config 模块无 providers，多半得到空链）
    try:
        mod = importlib.import_module(
            "data.plugins.astrbot_plugin_irmia_vision.tools.config"
        )
        chain = mod.resolve_provider_chain()
        if chain:
            logger.info("复用 irmia_vision 的 VL 降级链（%d 个模型）", len(chain))
            return chain
    except Exception:
        pass
    return None


def _provider_to_vl_config(provider: dict) -> dict:
    keys = provider.get("key", [])
    if isinstance(keys, list):
        api_key = keys[0] if keys else ""
    elif isinstance(keys, str):
        api_key = keys
    else:
        api_key = ""
    manual = _config.get("vl_model", {}) if isinstance(_config.get("vl_model"), dict) else {}
    return {
        "provider": provider.get("type", "openai_chat_completion"),
        "base_url": provider.get("api_base", "https://api.openai.com/v1"),
        "api_key": api_key,
        "model": provider.get("model", ""),
        "timeout": provider.get("timeout", manual.get("timeout", 120.0)),
    }


def _chain_from_self() -> list[dict]:
    """从本插件配置 + AstrBot 已保存模型解析降级链。"""
    providers: list[dict] = []
    if _context is not None:
        try:
            for prov in _context.get_all_providers():
                pc = getattr(prov, "provider_config", None)
                if isinstance(pc, dict) and pc.get("id"):
                    providers.append(pc)
        except Exception as e:
            logger.warning("读取 AstrBot provider 列表失败: %s", e)

    ids_raw = _config.get("vl_provider_ids", "") or ""
    p123 = [
        str(_config.get(k, "") or "").strip()
        for k in ("vl_provider_1", "vl_provider_2", "vl_provider_3")
    ]
    if any(p123):
        ids = [p for p in p123 if p]
    elif isinstance(ids_raw, str):
        ids = [x.strip() for x in ids_raw.replace("，", ",").split(",") if x.strip()]
    elif isinstance(ids_raw, list):
        ids = [str(x).strip() for x in ids_raw if str(x).strip()]
    else:
        ids = []

    if ids and providers:
        pmap = {p.get("id", ""): p for p in providers}
        chain = [_provider_to_vl_config(pmap[pid]) for pid in ids if pid in pmap]
        if chain:
            return chain
    if not ids and providers:
        return [_provider_to_vl_config(p) for p in providers]

    manual = _config.get("vl_model", {})
    if isinstance(manual, dict) and manual.get("api_key"):
        return [manual]
    return []


_chain_cache: Optional[list[dict]] = None


def get_chain() -> list[dict]:
    """获取 VL 降级链（优先复用 irmia_vision）。

    只缓存非空链：插件加载可能早于 provider 初始化，空链不缓存以便后续重试。
    """
    global _chain_cache
    if _chain_cache:
        return _chain_cache
    chain = _chain_from_irmia_vision() or _chain_from_self()
    if chain:
        _chain_cache = chain
        logger.info(
            "VL 降级链: %s",
            " -> ".join(f"{c.get('model','?')}@{c.get('base_url','')[:30]}" for c in chain),
        )
    else:
        logger.warning("未配置任何 VL 模型：定位/看图能力不可用，仅 OCR/记忆通道可用")
    return chain


def vl_available() -> bool:
    return bool(get_chain())


# ── VL 调用 ─────────────────────────────────────────────────────

def encode_for_vl(image, long_edge: int = VL_IMAGE_LONG_EDGE) -> tuple[str, float]:
    """压缩图片为 VL 输入。返回 (data_url, scale)——scale = 模型侧像素 / 原图像素，
    用于把模型给出的坐标换算回原图坐标。"""
    img = image.convert("RGB")
    w, h = img.size
    scale = 1.0
    if max(w, h) > long_edge:
        scale = long_edge / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{b64}", scale


async def ask(image, prompt: str, *, max_tokens: int = 4096,
              json_mode: bool = False) -> str:
    """调用 VL 模型（按降级链依次尝试），返回文本。全部失败抛异常。

    推理型模型注意：思考链会消耗 max_tokens，给太小会导致 content 为空
    （调用方曾用 64/128，思维链直接吃光配额）——所以默认 4096。
    content 为空时先原样重试一次，仍空才回退 reasoning_content 并加标记。
    """
    chain = get_chain()
    if not chain:
        raise RuntimeError("未配置 VL 模型")

    image_url, _scale = encode_for_vl(image)
    last_err: Optional[Exception] = None
    timeout = max(float(c.get("timeout", 120.0)) for c in chain)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for cfg in chain:
            if not cfg.get("api_key"):
                continue
            payload = {
                "model": cfg.get("model", ""),
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url",
                             # 尊重复用链上的 detail 配置（irmia_vision v1.0.6+）
                             "image_url": {"url": image_url,
                                           "detail": cfg.get("detail", "auto") or "auto"}},
                        ],
                    }
                ],
                "max_tokens": max_tokens,
            }
            if json_mode:
                payload["response_format"] = {"type": "json_object"}
            base_url = str(cfg.get("base_url", "")).rstrip("/")
            try:
                for _attempt in range(2):  # 空内容原样重试一次
                    resp = await client.post(
                        f"{base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {cfg['api_key']}"},
                        json=payload,
                    )
                    resp.raise_for_status()
                    msg = resp.json()["choices"][0]["message"]
                    content = (msg.get("content") or "").strip()
                    if content:
                        return content
                # 两次都空：回退思维链（DeepSeek 推理型），加标记便于甄别
                reasoning = (msg.get("reasoning_content") or "").strip()
                if reasoning:
                    return "[reasoning] " + reasoning
                last_err = ValueError(f"模型 {cfg.get('model','')} 返回空内容")
            except Exception as e:
                last_err = e
                logger.warning("VL 模型 %s 失败，尝试降级: %s", cfg.get("model"), e)
    raise RuntimeError(f"所有 VL 模型均失败: {last_err}")
