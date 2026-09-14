---
name: desktop-play
description: >
  桌面操控工作流。触发：需要操作 Windows 桌面软件或游戏——点击、输入、按键、窗口管理、读屏。
  核心原则：事实看 OCR，状态看 diff，VL 只做语义；能用 click(target=文字) 就别碰坐标。
  可用工具：look、scan_scene、click、type_text、press_key、scroll、drag、wait_change、window_action。
---

# 桌面操控工作流

## 决策树

```
要操作的目标有文字？
├─ 有 → click(target="界面上的确切文字")     ← 三级定位自动走（记忆→OCR→VL）
├─ 没有文字但知道大概位置 → look() 看界面 → 用返回的坐标 click(x, y)
└─ 图形场景（游戏/设计软件）→ scan_scene() 拿结构化元素清单再行动
```

## 关键规则

1. **永远不要自行换算坐标**。look/scan_scene 返回的 x/y 是屏幕原生像素，直接传给
   click(x, y)。看到 `coords.scale_factor` 不需要你乘除任何东西——插件内部已处理。
2. **事实看 OCR，状态看 diff，VL 只做语义**：
   - 界面上的文字、时间、数字 → 信 `ocr_elements`，别信 `vl_analysis`（VL 会幻觉）；
   - 操作是否生效 → 看返回的 `screen_changed`（有阈值过滤，光标闪烁不算）；
   - `vl_analysis` 只用来理解"这是什么界面、大概能干什么"。
3. **每次动作后检查 `screen_changed`**。为 false 说明可能没点中/没生效——
   别傻乎乎连按，先 `look` 或 `wait_change` 确认现场。
4. **等待用 `wait_change`**，不要盲 sleep。
5. **type_text 传 target**：让插件先真实点击聚焦再输入，中文自动走剪贴板通道。

## 图形/游戏场景须知

- OCR 读不出图形元素（门、楼梯、NPC、道具），这类目标用 `scan_scene()`：
  它返回每个元素的语义名称、类型、坐标、**是否带提示图标**。
- **领域知识**：RPG/SLG 里可互动物通常有视觉提示——头顶气泡/感叹号、
  描边高亮、悬浮变色。`scan_scene` 返回的 `has_icon=true` 是强判据。
- 游戏里 `press_key` 走扫描码通道（SDL2/pygame/DirectInput 兼容），
  方向键/WASD 直接可用；如果某游戏不认，优先确认游戏窗口是否前台
  （先 click 游戏画面或 window_action(action="focus")）。
- VL 给出的坐标是近似值，关键点击用 `click(target=...)` 让 hover-verify 复核，
  或点击后看 `screen_changed` 验证。

## 反模式

- ❌ 把 `look` 的 VL 描述当事实依据（它可能编造时间和物体）。
- ❌ 自己拿逻辑分辨率/缩放比去换算坐标。
- ❌ 连续盲目重试同一个动作而不重新读屏。
- ❌ 对"保存/删除/发送"类不可逆动作不做结果确认。
