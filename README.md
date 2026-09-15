# 🖱️ DeskHand v2 — 弥亚之手（视觉方案）

> *"VL 模型负责看懂要干什么，工程负责精确做到。"*

**astrbot_plugin_deskhand** 是一个 AstrBot 插件，让 LLM Agent 通过**截图 + 视觉模型**操控 Windows 桌面——任意软件皆可操作，不依赖应用是否暴露控件接口。

核心设计原则：**不纯依赖 VL 模型**——凡是确定性工程手段能做的（定位、验证、记忆），都不让 VL 做。定位分四层：**L0 UIA 控件树**（窗口模式先行，控件级精确且支持后台操作）→ **L1 元素记忆库** → **L2 本地 OCR** → **L3 VL 漏斗**。

> v1 曾是纯 UIA 方案（COM 线程模型在 AstrBot 下不可用而废弃）；v2.6.1 起 UIA 以「可选 L0 快速通道」形式回归：可用则精确直达、可后台执行，不可用则整条链路静默跳过，视觉方案仍是万能兜底。

---

## 三级定位引擎

```
click(target="保存按钮")
  │
  ├─ L0 UIA 控件树  窗口模式下先行：控件级精确坐标 + 后台 Invoke/SetValue（0 次模型调用）
  ├─ L1 元素记忆库  历史成功坐标 + 局部图像签名验证 → 命中即点（0 次模型调用）
  ├─ L2 本地 OCR    文字目标直接拿精确像素坐标（0 次模型调用，需可选依赖）
  └─ L3 VL 漏斗     VL 指出 3×3 格子 → 裁剪放大 → VL 指点像素 → 换算回屏幕坐标
                    （1-2 次调用，每次 ≤384 token）
```

点击前还有 **hover-verify**：在落点画标记局部截图，让 VL 确认「准星是否压在目标上」，不对则按 VL 建议修正一次再确认。动作后自动 **ImageChops diff** 验证画面是否变化。

## 工作方式：元素卡片 + 标注图，零运算

LLM 不需要算坐标、不需要记文字、不需要解读验证字段——一切机械劳动都在插件内部：

```
1. look(window="记事本")   → 编号元素卡片（约 1 秒，UIA + OCR + CV 三通道，免费）
     e1 [button] 导出到文件 (1062,889)
     e2 [checkbox] 启用开关 (1092,825)
     e3 [input] 文件名 (1317,754)
     e4 [text] 保存 (920,490)
   + 元素标注图（框和编号与卡片一一对应，主模型可直接看图，自行发现遗漏元素）
   + 截断时卡片末尾会写明「已截断：ocr 23/80、cv 17/31」——不会让你以为屏幕上就这么多
2. click(element="e1")     → UIA 控件：后台 InvokePattern 直接触发（不移动鼠标）；
                              非 UIA 元素：现场校验 → 弹窗遮挡/移动先 OCR 自愈，救不回就报 stale
   → 插件自动完成 定位→确认→执行→验证
3. 返回 verdict            → success / uncertain / failed + 一句中文结论
```

元素来源三通道：**UIA 控件**（青色，控件级精确，带 pattern 的可后台操作）、**OCR 文字**（精确）、**CV 候选框**（OpenCV 轮廓检测，凡有边框的东西都标出来，无语义；落在真实 UIA 控件内部的重复框自动跳过，但整窗容器不会吞掉它们）。

**40 个槽位按通道保底配额分配**（uia 12 / ocr 14 / cv 8，剩余按优先级补），不再“谁先加入谁占满”——实测全屏曾出现 31 个 CV 框被 OCR 全部挤掉、Agent 因此误判“没有可点的按钮”。每次截断都在卡片与 JSON 里如实告知。

OCR 还有**分块放大**重试：小字号（中位字高 <16px）时把画面切块放大 2× 再识别、合并回原图坐标（真值基准实测 12px 字错误率 57%→16%；常规大小的界面文字加放大无收益，会自动跳过不浪费一秒）。

## 九个工具

| 工具 | 功能 | 示例 |
|------|------|------|
| `look` | 看屏幕/窗口，编号元素卡片 + **元素标注图（多模态）** | `look(window="QQ")` |
| `scan_scene` | 场景结构识别（图形/游戏场景：VL 编号卡片 + 标注图） | `scan_scene(window="游戏")` |
| `click` | 点击（**element=eN 编号引用** / target 三级定位 / x,y 直点；UIA 元素后台执行） | `click(element="e2")` |
| `type_text` | 输入文本（UIA 输入框后台写入；否则中文走剪贴板） | `type_text(text="你好", element="e3")` |
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
pip install -r requirements.txt   # pillow / pywin32 / httpx / uiautomation
```

**UIA 控件通道（L0，随 requirements 默认安装）**：`uiautomation` 为纯 Python + comtypes，未安装时自动禁用 L0 层（仅记一条启动日志），不影响其余功能。

**可选增强（本地 OCR，强烈建议）**：

```bash
pip install winsdk                      # WinRT 系统 OCR（Windows 10+，推荐）
# 或
pip install rapidocr-onnxruntime        # 本地模型 OCR（跨平台）
```

**可选增强（CV 候选框检测）**：

```bash
pip install opencv-python-headless      # 轮廓检测：凡有边框的元素都框选标定（无语义）
```

## VL 模型配置（三选一）

1. **零配置**：同时安装了 [irmia_vision](https://github.com/irmia2026/astrbot_plugin_irmia_vision) 插件 → 自动复用它的 VL 降级链；
2. **下拉框**：WebUI 插件配置里 `vl_provider_1/2/3` 按优先级选 AstrBot 已保存模型；
3. **手动**：`vl_model` 填 OpenAI 兼容 API（base_url/api_key/model）。

推荐模型：`deepseek-v4-flash-vision-exp`（每张图 ≤384 token，单步成本约 0.001-0.006 元）。

## 定位通道对比

| | UIA（L0） | 视觉（L1-L3） |
|---|---|---|
| 启用条件 | 窗口模式 + 装了 uiautomation | 始终可用（OCR/VL 为可选增强） |
| 覆盖范围 | 仅暴露 UIA 的软件 | 任何能显示的软件 |
| 定位精度 | 控件级精确、实时重定位 | OCR 精确 / VL 漏斗+验证 近似 |
| 执行方式 | 有 pattern 的可后台执行（不移动鼠标） | win32 真实键鼠（需前台焦点） |
| 失败时 | 静默回落视觉路径 | 报 stale / uncertain，交 Agent 决策 |

两者是叠加关系：UIA 负责「有控件接口且能后台做」的部分，视觉负责其余全部场景。

## 注意事项

- 仅支持 Windows；需要桌面会话（远程桌面最小化时截图会黑屏）。
- **标注图（多模态）需要主模型支持图像输入**：AstrBot 会把图片喂给 provider 配置里 `modalities` 含 image 的聊天模型；纯文本模型自动只收到文字卡片，功能不受影响。
- 中文输入默认走剪贴板粘贴通道（实测最可靠），纯 ASCII 走 SendInput 逐键注入；配置项 `input_method` 可强制切换（auto/unicode/clipboard）。粘贴会短暂占用剪贴板，用后自动恢复**文本**内容（图片/文件等非文本内容无法恢复，请注意）。
- 键盘注入只对「系统前台焦点」生效：`type_text` 建议传 `target` 或 `element` 让插件先处理焦点；UIA 输入框走 `ValuePattern.SetValue` 后台写入，无需聚焦（不需要键盘焦点）。
- UIA 后台操作**不移动鼠标**；但部分框架（如 WinForms 经 MSAA 桥）在 SetValue/Invoke 时会把目标窗口带到前台，插件会把这种情况如实回传为 `focus_changed=true`，不会谎报「完全静默」。
- UIA 元素即使在窗口被遮挡时也能拿到（控件树不依赖可见像素），但此时的像素级验证不可信，插件会自动改用「状态回读 / 树结构变化」验证。
- 操作坐标一律为屏幕原生像素（插件内部已处理 DPI），LLM 无需也不应自行换算坐标。
- 这是桌面级操作能力，请注意授权范围——任何能给 bot 发消息的人理论上都能驱动你的电脑。
- 记忆库文件：插件数据目录 `deskhand_memory.db`，删除即清空记忆。

## 许可

AGPL-3.0 — 弥亚庄园出品 🏰
