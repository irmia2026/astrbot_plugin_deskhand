# 🏗️ DeskHand v2 架构设计（视觉方案）

> 版本 v2.4.0 · 2026-08 · 全面转向视觉方案

---

## 目录结构

```
astrbot_plugin_deskhand/
├── metadata.yaml              # 插件元信息
├── main.py                    # Star 入口：配置装配 + 工具注册 + 生命周期清理
├── requirements.txt           # Pillow / pywin32 / httpx
├── _conf_schema.json          # VL 模型配置 + 定位与行为开关
├── engine/
│   ├── desktop.py             # 单线程执行器、DPI 感知、窗口枚举、虚拟屏原点、hwnd 记忆
│   ├── input.py               # win32 键鼠（SendInput UNICODE / 剪贴板粘贴 / 扫描码按键）
│   ├── ocr.py                 # 本地 OCR：WinRT（winsdk）→ RapidOCR → 无（可插拔）
│   ├── vl.py                  # VL 客户端：复用 irmia_vision 降级链或内置解析
│   ├── locate.py              # 三级定位引擎 + 网格标注 + hover-verify
│   ├── scene.py               # 元素快照注册中心（e1..eN 编号 → 坐标，TTL 120s）
│   ├── detect.py              # CV 候选框检测（OpenCV 轮廓 + 几何过滤，可选依赖）
│   ├── memory.py              # 元素记忆库（SQLite + aHash 图像签名）
│   └── verify.py              # ImageChops 图像 diff + wait_for_change
├── tools/__init__.py          # 9 个工具 + FunctionTool 注册工厂
└── skills/desktop-play/       # 领域知识 SKILL.md（决策树/反模式/游戏场景须知）
```

## 核心设计原则

**VL 只做语义，工程做空间。** VL 模型（如 deepseek-v4-flash-vision-exp）每张图被压到
~800×800 / ≤384 token，空间定位是其最弱环节；所以坐标尽量由确定性手段产出：

| 层级 | 手段 | VL 调用次数 | 精度 |
|------|------|------------|------|
| L1 | 元素记忆库（历史坐标 + aHash 签名验证） | 0 | 精确（历史落点） |
| L2 | 本地 OCR 文字匹配 | 0 | 像素级 |
| L3 | VL 网格漏斗（粗定位格子 → 裁剪放大 → 指点像素） | 1-2 | 近似（配合 hover-verify） |

## 一次 click 的完整链路

```
click(target="保存")
  → desktop 线程截窗口图（DPI 感知，坐标=像素）
  → L1 记忆命中？（签名汉明距离 ≤10 直接返回）
  → L2 OCR 找文字（跨池最佳匹配：词级精确 > 行级精确 > 包含，防同行多按钮误点）
  → L3 VL 漏斗（3×3 网格 → 裁剪 → 像素坐标，按预缩放比例换算回屏幕）
  → hover-verify：落点画红色准星，局部 320×320 截图让 VL 确认/给修正量（最多 2 次）
  → win32 分段移动 + 点击
  → ImageChops diff 前后截图（~10ms/1080p），返回 changed/percent/region
  → 记忆库 upsert（成功 hits+1 并更新坐标/签名；失败只 fails+1 不覆盖旧记忆；连续失败 3 次淘汰）
```

## 关键工程决策

| 决策 | 理由 |
|------|------|
| 单线程 ThreadPoolExecutor 执行所有桌面操作 | 鼠标是全局共享资源，串行天然防竞态；无 COM 依赖（v1 的 UIA 线程炸弹随之消失） |
| SetProcessDPIAware | GetWindowRect 与 ImageGrab 坐标一致（HiDPI 不错位） |
| VL 图片预缩放到长边 768 | DeepSeek 会把图压到 ~800×800；预缩放让「模型坐标→屏幕坐标」换算确定 |
| UNICODE 文本注入（SendInput KEYEVENTF_UNICODE）+ 中文走剪贴板粘贴 | keybd_event 实测打不出字；剪贴板通道对中文 100% 可靠（含保存/恢复） |
| 按键走扫描码通道（KEYEVENTF_SCANCODE + 扩展键标记） | SDL2/pygame/DirectInput 只认扫描码，VK 注入游戏收不到 |
| SetProcessDPIAware | GetWindowRect 与 ImageGrab 坐标一致（HiDPI 不错位） |
| 全屏坐标用虚拟屏原点换算 | 多显示器副屏在主屏左/上时原点为负，不换算则全屏坐标整体偏移 |
| 窗口 hwnd 记忆 + class_name 复核 | min/restore 不重复匹配标题；hwnd 被 OS 复用时自动失效防误关 |
| FunctionTool 子类化 + call() 重写 | AstrBot v4.16+ 执行器原生支持，不依赖 star_manager 的 partial 回绑时机 |
| 显式设置 handler_module_path | 保证插件卸载/重载时工具被正确清理 |
| VL 降级链优先复用 irmia_vision | 两插件并存时零重复配置；软依赖，缺失自动回退内置解析 |

## 成本模型（deepseek-v4-flash-vision-exp 高峰价）

| 场景 | VL 调用 | 单步成本 |
|------|---------|---------|
| 记忆命中 | 0 | ~0 元 |
| OCR 命中 | 0（+1 次 hover-verify 可选） | ~0-0.001 元 |
| VL 漏斗 | 1-2 + 1 确认 | ~0.003-0.006 元 |

## 版本历史

| 版本 | 日期 | 内容 |
|------|------|------|
| v1.0.0 | 2026-06-12 | UIA 控件树方案（已废弃：COM 线程模型在 AstrBot 下不可用） |
| v2.0.0 | 2026-08 | 全面转向视觉方案：三级定位 + 记忆库 + hover-verify + diff 验证 |
| v2.1.0 | 2026-08 | 中文输入修复（SendInput/剪贴板双通道）；look 引导 LLM 用 target 而非自行换算坐标 |
| v2.1.1 | 2026-08 | 窗口管理修复（最小化窗口误过滤、hwnd 记忆、restore 状态机、screen_changed 补齐、坐标空间标注） |
| v2.2.0 | 2026-08 | scan_scene 场景结构识别；press_key 扫描码通道；desktop-play SKILL.md；VL max_tokens 配额修复 |
| v2.2.1 | 2026-08 | 多视角评审修复 35 项：scroll 方向反转、剪贴板 finally、OCR 跨池匹配、虚拟屏原点、坐标钳制等 |
| v2.2.2 | 2026-08 | 思维链隔离（结构化调用禁用 CoT 回退，防「假通过」）；空 content 继续降级链；链缓存 TTL；OCR 文字差异层；hover-verify 异常降级 |
| v2.3.0 | 2026-08 | 元素卡片机制：look/scan_scene 注册编号快照（e1..eN），click(element=eN) 直接引用，Agent 零坐标运算；动作结果压缩为 verdict 三档中文结论；look 默认只跑免费 OCR（VL 改按需） |
| v2.4.0 | 2026-08 | 多模态 look/scan_scene：返回 CallToolResult 附元素标注图（AstrBot 缓存后喂给图像模态主模型）；CV 候选框检测通道（OpenCV 轮廓，凡有边框必标）；OCR 多尺度重试（小字号自动放大，坐标不外泄） |
