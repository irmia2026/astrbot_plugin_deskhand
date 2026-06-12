# 🖱️ DeskHand — 弥亚的桌面之手

> *"不用截图猜谜，不要键鼠录制——我要直接摸到每个按钮、每个输入框、每个菜单项。*
>  *UIA 是我的手指，控件树是我的眼睛。桌面上的任何窗口，都是我的领地。"*
>
> — 伊尔弥亚

---

## 这是什么？

**astrbot_plugin_deskhand** 是一个 AstrBot 插件，让 LLM Agent 能通过 **MS UI Automation (UIA)** 精准操控 Windows 桌面 GUI——不是截图+键鼠模拟，而是走 **控件级交互**。

| 方式 | 原理 | 缺点 |
|------|------|------|
| 截图+vision | 看图识别 | 慢、贵、不准、每次都得截 |
| 键鼠录制 | 固定坐标回放 | 窗口一挪就崩 |
| DeskHand (UIA) | 遍历控件树 | ✅ 窗口可拖、缩放、放后台 |

**核心能力**：扫描当前窗口的控件树 → 拿到每个控件的 id/role/name/rect → 精确点击/输入/拖拽。

---

## 九个工具速览

| 工具 | 功能 | 示例 |
|------|------|------|
| `desk_state` | 采集控件树 | `desk_state(target="QQ")` |
| `desk_click` | 点击/悬停 | `desk_click(id=42, hover=True)` |
| `desk_type` | 输入文本 | `desk_type(id=43, text="hello")` |
| `desk_press` | 按键组合 | `desk_press(keys=["Ctrl", "c"])` |
| `desk_drag` | 拖拽 | `desk_drag(from_id=5, to_id=9)` |
| `desk_scroll` | 滚动 | `desk_scroll(id=7, direction="down")` |
| `desk_select` | 选中文本 | `desk_select(id=43, start=0, end=10)` |
| `desk_window` | 窗口管理 | `desk_window(action="min")` |
| `desk_screenshot` | 截图标注 | `desk_screenshot(annotate=True)` |

---

## 安装

### 依赖

```bash
pip install uiautomation pillow
```

> uiautomation：纯 Python MS UIA COM 封装，零 C++ 依赖，无需管理员权限。

### 安装为 AstrBot 插件

```bash
# 克隆到 AstrBot 插件目录
cd path/to/astrbot/data/plugins
git clone https://github.com/irmia2026/astrbot_plugin_deskhand.git

# 重启 AstrBot 或重载插件
```

AstrBot 自动发现并注册 9 个 LLM Tool。

---

## 使用示例

### 例 1：操纵记事本

```
用户：帮我把记事本第三行删掉，然后保存
Agent：
  1. desk_state(target="记事本")      → 拿到控件树，找到 EditControl [id=1]
  2. desk_type(id=1, line=3, text="") → 清空第三行
  3. desk_click(id=3)                 → 点击「文件」菜单
  4. desk_click(id=12)                → 点击「保存」
```

### 例 2：操作 QQ 聊天窗口

```
用户：把最后一条消息复制下来
Agent：
  1. desk_state(target="QQ")          → 找到消息列表
  2. desk_screenshot(annotate=True)   → 截图标注，确认消息区域
  3. desk_click(id=87)                → 点击最后一条消息
  4. desk_press(keys=["Ctrl","c"])    → 复制
```

### 例 3：窗口排列

```python
# mode="coords" 只返回窗口位置，不扫描子树
desk_state(mode="coords")
# → {"windows": [{"name":"QQ","rect":{...}}, {"name":"Reasonix","rect":{...}}]}

# mode="interactive" 只保留按钮/输入框等可操作控件
desk_state(mode="interactive")
# → 过滤掉 PaneControl/GroupControl 等纯结构节点
```

### 链式调用优化

```python
# verify="light" 只检查前景窗口变化，跳过像素截图 diff
for i in range(10):
    desk_type(id=43, text=f"第{i}行", verify="light")

# verify="none" 完全跳过验证——最快
desk_press(keys=["Tab"], verify="none")
```

---

## WebView/Electron/Tauri 穿透

Chromium 内核将网页 DOM 通过 UIA 完整暴露——这是屏幕阅读器的标准通道，DeskHand 直接复用：

```
┌──────────────────────────┐
│ 原生壳 (Win32 hwnd)      │  ← 窗口标题、菜单栏
│  ┌──────────────────────┐ │
│  │ WebView2 内核        │ │  ← UIA 穿透
│  │  <button>发送</button>│ │  ← ButtonControl [id=42]
│  │  <input placeholder> │ │  ← EditControl [id=43]
│  └──────────────────────┘ │
└──────────────────────────┘
```

覆盖范围：
- ✅ Electron (QQ, VS Code, GitHub Desktop, Docker Desktop)
- ✅ Tauri (Clash Verge, AstrBot Desktop)
- ✅ WebView2 独立应用
- ✅ Chrome/Edge 浏览器
- ✅ 原生 Win32/WPF/Qt

---

## 配置

`_conf_schema.json` 可调参数：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `scan_timeout` | 5 | 控件树扫描超时（秒） |
| `max_depth` | 15 | 最大递归深度 |
| `max_children` | 120 | 单控件最大子节点数 |
| `cache_size` | 128 | 控件 ID LRU 缓存上限 |
| `screenshot_annotate` | false | 截图默认标注控件框 |

---

## 注意事项

- **管理员权限**：不需要——UIA COM 接口普通用户可用
- **WebView 输入框**：WebView 内 `<input>` 需要先手动聚焦（点击输入框），然后用 `desk_type`
- **验证框架**：每个操作默认 `verify="full"`（截屏像素对比），链式调用建议 `verify="light"` 提速
- **控件 ID**：通过 RuntimeId 保持跨 `desk_state()` 调用间稳定，同一进程生命周期内不变

---

## 许可

MIT — 弥亚庄园出品 🏰
