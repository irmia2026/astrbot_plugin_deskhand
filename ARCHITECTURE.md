# 🏗️ DeskHand 架构设计

> 版本 v1.0.0 · 2026-06-12 · opencode

---

## 目录结构

```
astrbot_plugin_deskhand/
├── metadata.yaml              # AstrBot 插件元信息
├── main.py                    # Star 插件入口，注册 9 个 LLM Tool
├── requirements.txt           # uiautomation, pillow
├── _conf_schema.json          # 配置 Schema（5 项）
├── logo.png                   # 插件图标
├── README.md                  # 使用文档
├── ARCHITECTURE.md            # 本文件
├── engine/                    # 核心引擎
│   ├── scanner.py             # ① 控件树采集
│   ├── actuator.py            # ② 操作执行器
│   ├── verifier.py            # ③ 多维信号自验证
│   └── cache.py               # ④ 控件 ID 稳定映射
└── tools/                     # 9 个 LLM Tool 模块
    ├── desk_state.py          # 采集控件树
    ├── desk_click.py          # 点击/悬停
    ├── desk_type.py           # 输入文本
    ├── desk_press.py          # 按键组合
    ├── desk_drag.py           # 拖拽
    ├── desk_scroll.py         # 滚动
    ├── desk_select.py         # 选中文本
    ├── desk_window.py         # 窗口管理
    └── desk_screenshot.py     # 截图标注
```

---

## 四大引擎

### ① scanner.py — 控件树采集

**职责**：将 UIA 控件树结构化，输出 LLM 可理解的 JSON。

**核心流程**：
```
uiautomation.GetRootControl()
  → 找到活跃窗口（或 target 匹配）
  → 递归遍历子树
  → 每个控件输出 {id, role, name, value, rect, enabled, children}
```

**关键设计**：
- **双模式**：`mode="interactive"` 过滤纯结构节点（PaneControl/GroupControl），只保留按钮、输入框等；`mode="coords"` 只返回窗口名+rect 列表，不递归
- **WebView 穿透**：遇到 `Chrome_RenderWidgetHostHWND` 不截断，深入子控件拿到网页内的 button/input
- **超时保护**：单窗口 ≤5s，超时截断
- **深度限制**：max_depth=15，max_children=120
- **配置缓存**：`_config_cache` 模块级变量，仅首次读文件

### ② actuator.py — 操作执行器

**职责**：将 `desk_click(id=42)` 翻译为 Windows API 调用。

**核心技术栈**：
```
UIA ControlFromHandle → GetClickablePoint → SetCursorPos + mouse_event
UIA ValuePattern.SetValue / SendKeys → 文本输入
UIA ScrollPattern / mouse_event(WHEEL) → 滚动
UIA TextPattern → 文本选中
uiautomation SendKeys → 按键（含 {Ctrl}a 等组合）
win32gui.SetWindowPos / ShowWindow → 窗口管理
```

**操作前 precheck**：控件 handle 仍有效、仍 enabled。

**验证分级**：
| 级别 | 采集内容 | 耗时 |
|------|---------|------|
| `verify="full"` | 前景窗口句柄/标题 + 光标 + UIA快照 + 截图像素对比 | ~300ms |
| `verify="light"` | 前景窗口句柄/标题 + 光标 | ~50ms |
| `verify="none"` | 无 | <1ms |

### ③ verifier.py — 多维信号自验证

**职责**：操作前后采集 5 类信号并结构化对比。

**信号维度**：
1. **前景窗口**：句柄 + 标题是否变化
2. **光标位置**：(x, y) 是否移动
3. **UIA 快照**：获焦控件 + 关键控件树是否变化
4. **视觉变化**：操作前后截图逐像素对比（仅 full 模式）
5. **控件值**：Edit/Document 控件的文本内容变化

**容错设计**：
- 所有采集异常静默捕获，写入 `errors[]` 数组
- `visual_changed` 为 None 表示未截屏（light 模式）
- `pixel_diff` 转灰度图加速，阈值 16

### ④ cache.py — 控件 ID 稳定映射

**职责**：保证多次 `desk_state()` 调用间，同一个按钮的 id 不变。

**原理**：
```
UIA RuntimeId（int tuple，进程生命周期内不变）
  → ControlCache {id: RuntimeId}
  → 下次扫描同一 RuntimeId 复用旧 id
```

**线程安全**：所有可变操作受 `threading.Lock` 保护。

**LRU 淘汰**：上限 128 条（可通过 `_conf_schema.json` 配置）。

---

## 设计约束

| # | 约束 | 理由 |
|---|------|------|
| M1 | 不依赖视觉模型 | vision 可选，价格贵且慢 |
| M2 | 底层用 `uiautomation` | 纯 Python，无 C++ 编译，零管理员权限 |
| M3 | 独立插件 | 不放 devkit 工具箱，独立发布 |
| M4 | RuntimeId 做 ID | 跨调用的控件标识稳定性 |
| M5 | state() ≤2000 token | 一屏内，避免吃掉上下文 |
| M6 | 超时保护 | UIA 阻塞会卡死 Agent |
| M7 | precheck | 控件可能在使用前被销毁 |
| M8 | AstrBot 发布规范 | metadata.yaml + _conf_schema.json + logo.png |

---

## Chromium 穿透原理

Chromium 内核将网页 DOM 通过 UIA Provider 暴露为控件树——屏幕阅读器（Narrator/NVDA）的标准通道：

```
DOM                     UIA Control
─────────────────────────────────────────
<button>点击</button> → ButtonControl
<input type="text">    → EditControl (含 ValuePattern)
<a href="...">         → HyperlinkControl
<select>               → ComboBoxControl
<div role="listbox">   → ListControl
```

**检测逻辑**（`_is_chromium_host`）：
```python
_CHROMIUM_HOST_CLASSES = {
    "Chrome_RenderWidgetHostHWND",  # Chromium/WebView2
    "Chrome_WidgetWin_0",           # 旧版 Chromium
}
```

遇到这些 ClassName 时不截断遍历，正常递归子控件——网页内所有交互元素自动暴露。

**覆盖应用**：Electron (QQ/VS Code/Docker Desktop) · Tauri (Clash Verge) · WebView2 · Chrome/Edge · 原生 Win32/WPF/Qt

---

## 工具注册

AstrBot 通过 `FunctionTool` 注册到 LLM：

```python
FunctionTool(
    name="desk_state",
    description="扫描窗口控件树。target=窗口名过滤；mode=interactive只返交互控件/coords只返坐标。",
    parameters={...},
    handler=_desk_state_handler,
)
```

handler 层负责：参数解析 → 调用 engine → 结构化返回 → `_verify` 注入。

---

## 版本历史

| 版本 | 日期 | 内容 |
|------|------|------|
| v1.0.0 | 2026-06-12 | 初始发布：9 工具 + 4 引擎 + verify 验证框架 |
