# 🏗️ DeskHand v2 架构设计（视觉方案）

> 版本 v2.0.0 · 2026-08 · 全面转向视觉方案

---

## 目录结构

```
astrbot_plugin_deskhand/
├── metadata.yaml              # 插件元信息
├── main.py                    # Star 入口：配置装配 + 工具注册
├── requirements.txt           # Pillow / pywin32 / httpx
├── _conf_schema.json          # VL 模型配置 + 定位与行为开关
├── engine/
│   ├── desktop.py             # 单线程执行器、DPI 感知、窗口枚举、截图
│   ├── input.py               # win32 键鼠（UNICODE 文本注入 / shift 状态 / 水平滚轮）
│   ├── ocr.py                 # 本地 OCR：WinRT（winsdk）→ RapidOCR → 无（可插拔）
│   ├── vl.py                  # VL 客户端：复用 irmia_vision 降级链或内置解析
│   ├── locate.py              # 三级定位引擎 + 网格标注 + hover-verify
│   ├── memory.py              # 元素记忆库（SQLite + aHash 图像签名）
│   └── verify.py              # ImageChops 图像 diff + wait_for_change
└── tools/__init__.py          # 8 个工具 + FunctionTool 注册工厂
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
  → L2 OCR 找文字（行级优先，词级次之）
  → L3 VL 漏斗（3×3 网格 → 裁剪 → 像素坐标，按预缩放比例换算回屏幕）
  → hover-verify：落点画红色准星，局部 320×320 截图让 VL 确认/给修正量（最多 2 次）
  → win32 分段移动 + 点击
  → ImageChops diff 前后截图（~10ms/1080p），返回 changed/percent/region
  → 记忆库 upsert（成功 hits+1，连续失败 3 次淘汰）
```

## 关键工程决策

| 决策 | 理由 |
|------|------|
| 单线程 ThreadPoolExecutor 执行所有桌面操作 | 鼠标是全局共享资源，串行天然防竞态；无 COM 依赖（v1 的 UIA 线程炸弹随之消失） |
| SetProcessDPIAware | GetWindowRect 与 ImageGrab 坐标一致（HiDPI 不错位） |
| VL 图片预缩放到长边 768 | DeepSeek 会把图压到 ~800×800；预缩放让「模型坐标→屏幕坐标」换算确定 |
| UNICODE 文本注入（KEYEVENTF_UNICODE） | 中文/任意字符不依赖键盘布局与输入法 |
| VkKeyScan 保留 shift 状态位 | "!" 等字符正确按下 Shift（v1 的 bug） |
| MOUSEEVENTF_HWHEEL | 水平滚动用水平轮（v1 用垂直轮） |
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
