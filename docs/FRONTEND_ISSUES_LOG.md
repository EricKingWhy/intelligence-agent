# 前端问题登记簿（实时更新）

> **用途**：真实浏览器点击测试 + 日常使用中发现的任何问题，实时登记在此。
> 修 bug 时照着本文档逐条处理；修完把状态改为 `已修复` 并补上 commit。
>
> 状态词汇：`未修复` / `修复中` / `已修复（commit）` / `不改（理由）`
> 严重级：`P0 功能不可用` / `P1 功能可用但体验破损` / `P2 边角 / 打磨`

---

## 问题清单

### BUG-001 分叉按钮 422：`turn.step_id` 不是后端要的用户消息 seq 【P0 · 未修复】

**发现时间**：2026-09-10 真实浏览器点击测试（会话 28eb3302，点第 2 轮的「分叉」）

**现象**：点击用户消息上的「分叉」按钮 → `POST /api/sessions/{id}/forks` 返回 **422**，UI 无任何反馈（静默失败）。

**网络证据**：
- 请求体：`{"from_seq": 2}`
- 响应：`{"detail": "fork 边界 seq=2 不是父会话中的用户消息（可用边界: [1, 30, 46, 62, 199]）"}`

**根因**（`web/src/components/Conversation.tsx:290`）：
```tsx
onClick={() => onFork(turn.step_id)}
```
前端传的是 `turn.step_id`——这是投影层 `resolveStep` 为轮次分组**前端合成**的 step 值；而后端 fork 锚点要求的是**用户消息事件在 JSONL 里的 `seq`**（后端报错里列的可用边界 `[1, 30, 46, 62, 199]` 正是该会话 5 条 user/message 的 seq）。真实数据里 `user/message` 事件的 `step_id=None`，两个语义完全不同。

**修复方向**（最小方案）：
1. 投影层：`projectUserMessage` 已经拿得到 `event.seq`（user/message 的 seq 就是合法锚点），在 `Turn` 上记录 `user_message_seq: number`（per-turn 事实，与 T9 turn_index 落当轮同一模式）。
2. `Conversation.tsx:290`：`onFork(turn.user_message_seq ?? turn.step_id)` 改为只传 `user_message_seq`；没有该字段的历史 turn 显示分叉按钮但点击时给出提示，或对缺锚点的轮不渲染按钮（不造假入口）。
3. 错误反馈：`handleFork`（`web/src/App.tsx:312`）的 `catch {}` 是**空吞**——422/409 用户毫无感知。至少 `setError(...)` 显示后端 detail；409（在途 run）单独提示「等当前 run 结束再分叉」。

**回归**：修完在真实会话上点每一轮的分叉，确认请求 `from_seq` 落在后端返回的可用边界列表内、200 后跳转到 child session。

---

### BUG-002 交接手册 `HANDOFF_WORKBUDDY_FRONTEND.md` 关于 T9 的结论已过期 【P2 · 已过时，需勘误】

**发现时间**：2026-09-10 真实浏览器测试

**现象**：手册写「T9 未完成——TurnView 不渲染轮次标签」，但实际页面已真实渲染「第 1 轮」~「第 6 轮」。

**根因**：commit `cddea36`（T9 轮次标签 UI）已在当前 HEAD 里，手册是基于旧快照写的。**workbuddy 若照手册做会重复实现。**

**待办**：更新手册——T9 标记为已完成（commit `cddea36`），把「你需要做的工作」章节替换为指向 BUG-001（分叉 422）的修复任务。

---

### OBS-001 续聊 run 失败：`model call failed: BadRequestError` 【观察项 · 非前端 bug】

**发现时间**：2026-09-10 测试「会话级模型切换后续聊」

**现象**：切到 `glm-5.3-flash` 后发消息 → 后端 `model/failed`（`BadRequestError`）→ `run/failed`。

**前端表现**（正常）：header 显示「失败」、第 6 轮 turn 渲染出来、无崩溃。前端行为正确。

**初步判断**：后端模型调用问题（provider `senseaudio` 对该模型返回 400），属后端/环境问题。前端已正确消费 `model/failed` + `run/failed` 终态。

**附注**：这次失败同时验证了 T7 的链路真实可用——`model/changed` 事件（seq=319，`from qwen3.8-27b → to glm-5.3-flash`）落库，续聊请求带上了 amend 档位（`model`/`agent_profile`/`reasoning_effort`）。

---

### OBS-002 preset 任务按钮只在空态显示 【观察项 · 符合设计】

三个 preset 按钮（FizzBuzz / todo.md / 目录结构）只在未选会话的空态渲染，选中会话后消失。符合「空态引导」设计，非 bug，登记备查。

---

## 已验证正常的交互（2026-09-10 真实点击，共 17 项）

| # | 交互 | 结果 |
| --- | --- | --- |
| 1 | 会话列表点击选择会话 | ✓ 319 事件加载，header 显示会话 ID / 状态 / tok |
| 2 | T9 轮次标签 | ✓ 「第 1 轮」~「第 6 轮」per-turn 正确渲染（`cddea36`） |
| 3 | 折叠按钮 | ✓ 变「已折叠 · 0 个工具 · 1 轮 · 5.1s」，内容收起 |
| 4 | Timeline → Chat 反向联动 | ✓ 点 `user/message 什么是rag` 行，chat 滚到目标轮并进入视口 |
| 5 | Timeline 行 → 事件详情 | ✓ StepDetail 打开，显示 seq=199 的 USER/MESSAGE（seq/time/event_id） |
| 6 | 密度四档切换 | ✓ `data-density` 即时生效，localStorage `ahi.traceDensity` 持久 |
| 7 | 主题切换 | ✓ dark ↔ light，`data-theme` 属性正确翻转 |
| 8 | Inspector 收起/展开 | ✓ 收起后面板移除，展开恢复，aria-pressed 正确 |
| 9 | 模型选择器打开/选择 | ✓ dialog + cmdk 列表（默认链 + 5 个模型），选中后按钮文案更新 |
| 10 | **T7 会话级模型切换** | ✓ 已选会话中选模型 → `POST /model` 200 `{status:changed}`，本地状态用响应 `model_id` 更新 |
| 11 | 权限模式选择器 | ✓ 三档（只读/工作区写入/完全访问），描述文案齐全 |
| 12 | Agent Profile 选择器 | ✓ 三档（通用/编程/研究审查），选择生效 |
| 13 | Reasoning Effort 选择器 | ✓ 三档（轻量/标准/深度），选择生效 |
| 14 | 思考块展开 | ✓ aria-expanded 翻转，reasoning 全文展开 |
| 15 | 续聊发送 | ✓ `POST /messages` 200，请求体带 amend 档位（model/agent_profile/reasoning_effort）|
| 16 | run 失败的 UI 呈现 | ✓ header「失败」+ turn 渲染，无崩溃（内容见 OBS-001）|
| 17 | 分叉按钮 | ✗ 422（见 BUG-001）|

**测试环境**：后端 `localhost:8000`（58 个真实会话）+ 前端 dev server `localhost:5173`，Chrome 经 CDP 驱动，点击 + 网络面板双重验证。
