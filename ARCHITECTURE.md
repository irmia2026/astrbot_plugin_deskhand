# 🏗️ DeskHand v2 架构设计（视觉为基础 + UIA L0 快速通道）

> 版本 v2.6.1 · 2026-08 · 视觉方案为基础，UIA 作为可选 L0 层回归

---

## 目录结构

```
astrbot_plugin_deskhand/
├── metadata.yaml              # 插件元信息
├── main.py                    # Star 入口：配置装配 + 工具注册 + 生命周期清理
├── requirements.txt           # Pillow / pywin32 / httpx
├── _conf_schema.json          # VL 模型配置 + 定位与行为开关
├── engine/
│   ├── desktop.py             # 单线程执行器、DPI 感知、STA COM 初始化、窗口枚举、虚拟屏原点、hwnd 记忆
│   ├── uia.py                 # UIA L0：控件树遍历、pattern 路由（Invoke/SetValue）、状态回读验证（可选依赖）
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
| L0 | UIA 控件树（窗口模式先行） | 0 | 控件级精确（带 pattern 可后台执行） |
| L1 | 元素记忆库（历史坐标 + aHash 签名验证） | 0 | 精确（历史落点） |
| L2 | 本地 OCR 文字匹配 | 0 | 像素级 |
| L3 | VL 网格漏斗（粗定位格子 → 裁剪放大 → 指点像素） | 1-2 | 近似（配合 hover-verify） |

L0 仅窗口模式启用（全屏无句柄，控件树无意义），且失败时静默回落 L1→L2→L3；
scan_scene（图形/游戏场景）不走 UIA。UIA 不可用（未装 uiautomation）时整层消失，其余链路不变。

## 一次 click 的完整链路

```
click(target="保存")
  → desktop 线程截窗口图（DPI 感知，坐标=像素）
  → L0 UIA 控件树（仅窗口模式）：控件级元素 + pattern 探测
  → L1 记忆命中？（签名汉明距离 ≤10 直接返回）
  → L2 OCR 找文字（跨池最佳匹配：词级精确 > 行级精确 > 包含，防同行多按钮误点）
  → L3 VL 漏斗（3×3 网格 → 裁剪 → 像素坐标，按预缩放比例换算回屏幕）
  → hover-verify：落点画红色准星，局部 320×320 截图让 VL 确认/给修正量（最多 2 次）
  → 执行路由：
       UIA 元素且控件带 pattern → 后台 Invoke/Toggle/ExpandCollapse/SelectionItem（不动鼠标）
       type_text 遇 ValuePattern → 后台 SetValue
       其余 → win32 分段移动 + 真实点击
  → 验证分层：状态回读（ToggleState/Value）> 树结构变化 > ImageChops 屏幕 diff（仅前台）
  → 记忆库 upsert（UIA 路径不写记忆：控件树本身就是实时真值）
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
| UIA 回归为 L0 而非主路径 | v1 因 COM 线程模型废掉；此次在 desktop 单线程内做 STA 初始化 + 惰性导入（模块级 COM 对象必须在执行线程创建），可用则快、不可用则整层消失 |
| UIA 动作时**重新遍历按中心距重定位**，不缓存 Control | Control/Pattern 官方禁止跨线程、跨调用持有；重定位天然免疫快照过期 |
| 标题栏 chrome 整棵子树 + 标题栏条带内关键词双规则过滤 | Win32/WinForms 的 chrome 按钮挂在 TitleBarControl 下，UWP 没有 TitleBarControl（实测）——只靠任一条会漏 |
| 后台操作不宣称「绝不抢焦点」，而是如实回传 focus_changed | WinForms TextBox 经 MSAA 桥 SetValue 实测会把窗口带到前台；“完全不静默”是错的，插件必须说实话 |
| UIA 通道优先，同位置 12px 内的 OCR 条目不再上图；落在 UIA 控件内部的 CV 框跳过 | 三通道叠加会互相稀释卡片（实测 WinForms 夹具：3 个真控件 + 15 个 CV 重复框） |
| UIA 动作前校验目标身份（名称相同 或 矩形 IoU ≥ 0.6） | UIA 元素没有图像签名可做新鲜度校验；界面翻页后同坐标可能已是**另一个输入框**，SetValue 回读会“成功”而内容是错的 |
| 验证判据顺序：任一正向证据成立即成功 | 旧写法“有可读状态就只认状态”会把“状态未变但控件树变了”的真成功报成 failed |
| UWP CoreWindow 类窗口需通过「中心点顶层窗口是不是它自己」校验 | UWP 最小化后会留下 visible=1 + rect 正常的幽灵 CoreWindow（实测其 rect 区域实际显示的是别的窗口）→ 不过滤就会截到错误内容 |
| 慢空树缓存（仅缓存耗时 >0.5s 的空结果，TTL 120s，最小化不入缓存） | Nahimic/输入体验等应用的 UIA 查询要 1.2-1.4s 才返回空树；而快速返回空的 Electron/WebView2 必须每次重探（无障碍树常在首次查询后才激活） |
| 元素槽位按**通道保底配额**分配，不再先到先得 | 实测：全屏 look 检出 31 个 CV 候选框，40 个槽位被 OCR 占满 → CV 一个不上图，Agent 得出「这里没有可点的按钮」的错误结论。CV 是“无文字控件”的唯一来源，必须保底（uia 12 / ocr 14 / cv 8，剩余按优先级补） |
| 截断必须留痕（卡片末尾 + element_budget 字段） | v2.4.0 的“…等共 N 个”在预切片后成了死代码；40 条读完就没了，Agent 以为“屏幕上总共就这 40 个东西” |
| CV 去重只由**真实控件**触发，容器级 UIA 元素不得吞 CV | QQ/Chromium 窗口模式下 UIA 只给一个覆盖整窗的根容器（rect 甚至超出屏幕）→ 29 个 CV 框全被清空。现在：面积 ≥60% 窗口、或无名且 ≥25% 窗口的 UIA 元素不参与吞并；容器元素还排到 UIA 队列末尾（不再抢 e1） |
| UIA 元素矩形钳制到「窗口 ∩ 屏幕」，越界则标记 `rect_clamped` | 实测 Chromium 根容器根矩形右边界 2911 > 屏宽 2560；不钳制则画框越界、去重判据也跟着错 |
| OCR 多尺度 = **分块放大**（整图放大已废弃） | 真值基准实测：整图放大在 10px 字上 CER 56.8%→95.1%（放大倍数顶到引擎 2600 边长上限 → 引擎内部又缩一次 = 双重重采样）；分块放大全面胜出：10px→21.0%、12px→16.0%。真实渲染（记事本缩放）同结论：12-13px CER 33.3%→16.7%，20px 无收益 |
| 多尺度触发线取中位字高 16px（不是拍脑袋的 12px，也不是激进的 22px） | 真机界面文字实测 16-20px：≥17px 加放大没用（那类错字是引擎对形近字的混淆），却要多花 1.35s/次全屏；取 16 让小字号屏幕/日志/长文本吃到提升，常规屏幕不白花时间 |

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
| v2.5.0 | 2026-08 | click(element) 现场校验+自愈：注册时存图像签名，点击前比对，失效先 OCR 重定位再点击，失败明确报 stale；CV 框窗口原点修复；屏外幽灵窗口过滤+同级优先非最小化；OCR 行级条目保中文整句；记忆库 app_key 改 exe 名防撞车；窗口遮挡告警；卡片/JSON 40 条一致 |
| v2.6.0 | 2026-08 | 幽灵窗根治：无效 rect 跳档 + 进程名兜底（QQ NT 标题=会话名先天找不到，按 QQ.exe 取最大窗口）+ 诚实报错带 hwnd/iconic；OCR 多尺度真实修复（模块级 Image 导入缺失导致从未生效 + 词数/中位字高双触发）；剪贴板还原 3×100ms 重试 + clipboard_restored 回传；记忆库清理 class_name 时代死数据 |
| v2.6.1 | 2026-08 | UIA L0 吸收：engine/uia.py 控件树遍历（STA COM + 惰性导入 + 空壳三级回退 + 深度/数量封顶 + 标题栏 chrome 过滤）；look 窗口模式 UIA 先行（青色元素，OCR 12px 去重、CV 内部框跳过）；click/type_text 执行路由（Invoke/Toggle/ExpandCollapse/SelectionItem、ValuePattern.SetValue）后台执行，失败静默回落 win32；验证分层（状态回读 > 控件树变化 > 屏幕 diff，仅前台）；焦点变化如实回传 focus_changed；对抗性评审修复：空壳回退 PID×矩形双过滤（防动作打到无关窗口）、UIA 目标身份校验（防静默写错输入框）、before 位图异常路径不泄漏、判据顺序修正；UWP 幽灵 CoreWindow 过滤；慢空树缓存 |
| v2.6.2 | 2026-09 | 反馈轮修复（Agent 实测现象驱动）：元素槽位改**通道保底配额**（uia 12/ocr 14/cv 8，剩余按优先级补）——不再先到先得，文字密屏下 CV 不再被饿死；截断双留痕（卡片末尾提示 + `element_budget` 字段，含每通道 shown/detected）；CV 去重只由真实控件触发（容器级 UIA 元素不得吞 CV，容器排到卡片末尾）；UIA 矩形钳制到窗口∩屏幕并标记 `rect_clamped`；OCR 多尺度从「整图放大（真机必然早退的死路径）」改为**分块放大**（真值基准：10px CER 56.8%→21.0%、真实渲染 12-13px 33.3%→16.7%），触发线按实测标定为中位字高 16px，新增 `ocr_multiscale` 配置与 `ocr_detail` 诊断 |
