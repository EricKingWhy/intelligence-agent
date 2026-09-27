# W-27 · 进度清单 Web+桌面渲染（四件套，同一 React 组件）
**目标仓库**：intelligence-agent-frontend（React Web；桌面由 Electron 直接承载既有 Web——#344 冻结）。

**类型/优先级**：P1 UI。**依赖**：W-26（服务端契约）。**范围**：`web/src/` 新增 PlanList 组件 + 接入会话视图；对应 vitest。实施前重读 W-26 票面、PRD §7.5、调研 `docs/research/2026-09-27-agent-progress-visualization-research.md`。

## 明确契约

渲染四件套（全行业收敛形态，缺一不可）：

1. **N/M 计数**：标题区显示「进程 M 项 · 已完成 N 项」；
2. **当前项高亮**：`in_progress` 项显示 `activeForm`（非 content）+ 进行中视觉标记（→ 或动效）；
3. **完成项**：划线 + 完成勾选标；
4. **可折叠列表**：默认展开当前项与未完成任务，完成项可整组折叠（zcode 实测 UI 同构）。

数据源：仅从服务端投影（W-26 的 `task/plan_updated` 重放）读取；WS/SSE 通道新增帧类型随 W-26 后端一并定（若 W-26 未定义传输帧，本票先对齐再动工）。**无清单会话不渲染该组件**（不留空壳）。

## 工作指令

1. 先写组件测试：三态混合清单渲染四件套齐全；空清单不渲染；双 in_progress 的脏数据**渲染端容错**（高亮第一个，不崩——服务端已硬校验，容错仅防御旧数据）。
2. 更新时无闪烁：整表替换用 id 对齐做 reconcile，已完成项不重新挂载。
3. 桌面不单独动工：Electron 承载 Web 后此组件自动可用（W-15 验收时连带检查）。

## 验收

- vitest 覆盖上述判据；真实会话触发 `update_plan` 后 UI 同步无闪烁（可录屏/截图证据）。
- tsc -b 无错，按 V3.1-lite 记录冻结树与 review。

**不做**：TUI（W-28）、拖拽排序、依赖图可视化、清单编辑 UI（首版只读展示，编辑权在 agent/工具侧）。

**成熟参考/复用**：zcode 桌面 UI（实测截图：计数 + 折叠 + 当前项前缀 + 完成划线）、Cline task tracker、Claude Code TaskList 渲染同构（调研 P §收敛点）。
