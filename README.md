# 🖱️ DeskHand v2 — 弥亚的桌面之手（视觉方案）

> *"VL 模型负责看懂要干什么，工程负责精确做到。"*

**astrbot_plugin_deskhand** 是一个 AstrBot 插件，让 LLM Agent 通过**截图 + 视觉模型**操控 Windows 桌面——任意软件皆可操作，不依赖应用是否暴露控件接口。

v2 相对 v1（UIA 控件树方案）全面重写：不再依赖 MS UI Automation，改为视觉定位 + win32 键鼠。核心设计原则：**不纯依赖 VL 模型**——凡是确定性工程手段能做的（定位、验证、记忆），都不让 VL 做。

---

## 三级定位引擎

```
click(target="保存按钮")
  │
  ├─ L1 元素记忆库  历史成功坐标 + 局部图像签名验证 → 命中即点（0 次模型调用）
  ├─ L2 本地 OCR    文字目标直接拿精确像素坐标（0 次模型调用，需可选依赖）
  └─ L3 VL 漏斗     VL 指出 3×3 格子 → 裁剪放大 → VL 指点像素 → 换算回屏幕坐标
                    （1-2 次调用，每次 ≤384 token）
```

点击前还有 **hover-verify**：在落点画标记局部截图，让 VL 确认「准星是否压在目标上」，不对则按 VL 建议修正一次再确认。动作后自动 **ImageChops diff** 验证画面是否变化。

## 九个工具

| 工具 | 功能 | 示例 |
|------|------|------|
| `look` | 看屏幕/窗口（VL 分析 + OCR 文字坐标清单） | `look(window="QQ")` |
| `scan_scene` | 场景结构识别（图形/游戏场景：元素语义+类型+坐标+提示图标，合并 OCR） | `scan_scene(window="游戏")` |
| `click` | 点击（target 三级定位 / x,y 直点） | `click(target="发送")` |
| `type_text` | 输入文本（中文自动走剪贴板，无障碍） | `type_text(text="你好", target="输入框")` |
| `press_key` | 组合键（**扫描码通道**，游戏/SDL2/DirectInput 兼容） | `press_key(keys=["ctrl","s"])` |
| `scroll` | 滚动（垂直/水平） | `scroll(direction="down")` |
| `drag` | 坐标拖拽 | `drag(x1=100,y1=200,x2=300,y2=400)` |
| `wait_change` | 等画面变化（替代盲 sleep） | `wait_change(timeout=5)` |
| `window_action` | 窗口管理（hwnd 记忆、restore 状态保护） | `window_action(action="focus", title="记事本")` |

## 适用边界

```
擅长  文字为主、位置固定 → 聊天、填表、点按钮、读日志
可用  图形场景（游戏/CAD）→ 先 scan_scene 结构化识别，再操作
限制  UAC 提权窗口 / 远程桌面最小化 → 物理限制，不可用
```

## 安装

```bash
cd path/to/astrbot/data/plugins
git clone https://github.com/irmia2026/astrbot_plugin_deskhand.git
pip install -r requirements.txt   # pillow / pywin32 / httpx
```

**可选增强（本地 OCR，强烈建议）**：

```bash
pip install winsdk                      # WinRT 系统 OCR（Windows 10+，推荐）
# 或
pip install rapidocr-onnxruntime        # 本地模型 OCR（跨平台）
```

## VL 模型配置（三选一）

1. **零配置**：同时安装了 [irmia_vision](https://github.com/irmia2026/astrbot_plugin_irmia_vision) 插件 → 自动复用它的 VL 降级链；
2. **下拉框**：WebUI 插件配置里 `vl_provider_1/2/3` 按优先级选 AstrBot 已保存模型；
3. **手动**：`vl_model` 填 OpenAI 兼容 API（base_url/api_key/model）。

推荐模型：`deepseek-v4-flash-vision-exp`（每张图 ≤384 token，单步成本约 0.001-0.006 元）。

## 与 v1（UIA 方案）的对比

| | v1 UIA | v2 视觉 |
|---|---|---|
| 覆盖范围 | 仅暴露 UIA 的软件 | 任何能显示的软件 |
| 定位精度 | 控件级精确 | OCR 精确 / VL 漏斗+验证 近似 |
| 依赖 | uiautomation（COM 线程敏感） | pillow + pywin32 + httpx |
| 模型 | 纯文本模型即可 | 需要 VL 模型（很便宜） |

## 注意事项

- 仅支持 Windows；需要桌面会话（远程桌面最小化时截图会黑屏）。
- 中文输入默认走剪贴板粘贴通道（实测最可靠），纯 ASCII 走 SendInput 逐键注入；配置项 `input_method` 可强制切换（auto/unicode/clipboard）。粘贴会短暂占用剪贴板，用后自动恢复原内容。
- 键盘注入只对「系统前台焦点」生效：`type_text` 建议传 `target` 让插件先真实点击聚焦。
- 操作坐标一律为屏幕原生像素（插件内部已处理 DPI），LLM 无需也不应自行换算坐标。
- 这是桌面级操作能力，请注意授权范围——任何能给 bot 发消息的人理论上都能驱动你的电脑。
- 记忆库文件：插件数据目录 `deskhand_memory.db`，删除即清空记忆。

## 许可

AGPL-3.0 — 弥亚庄园出品 🏰
