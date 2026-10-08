# W-27.1 · 进度清单窗口化折叠（ZCode 对齐，渲染层）

**GitHub issue**：[#864](https://github.com/EricKingWhy/intelligence-agent/issues/864)（#344 子票；`enhancement` + `ready-for-agent`，未认领）
**实现分支**：`codex/issue-864-plan-list-window-fold`（基于 `origin/main` @159bb527）
**类型 / 优先级**：渲染层增强（无数据面改动）。
**依赖**：无（W-26/#380、W-27/#381、W-28/#382、W-29/#383 均已关单）。
**家族**：进度清单族 W-26~W-29（#380–#383，全部 CLOSED）。

## 背景与动机

用户以 ZCode 桌面端进度面板为目标观感（「进程 11/17 · 前面 12 项 · 待处理 2 项 · → / ○ / ✓划线」）。经查重，该设计在本仓已有完整实现，**不需要重复实现**：

- W-26/#380 服务端契约：`update_plan` 整表覆盖 + 单 in_progress 硬校验 → `task/plan_updated` 事件 → `derive_plan` 投影（`src/agent_harness/session/plan.py`、`src/agent_harness/tools/update_plan.py`）；
- W-27/#381 Web+桌面渲染：`web/src/components/PlanList.tsx`，钉在会话滚动区上方（`Conversation.tsx:392`），真机证据已归档；
- W-28/#382 TUI 薄渲染（ctrl+t 折叠）；
- W-29/#383 压缩锚点 + 重注入：`context/builder.py::_should_inject_plan`（事件驱动窗口 + 周期兜底 + 压缩后恒注入）。

规格冻结在 `docs/PRD_LONG_TASK_CONTEXT_MANAGEMENT.md` §7；同题调研见 `docs/research/2026-09-27-agent-progress-visualization-research.md`。

2026-10-08 对 ZCode 本机 bundle（`resources/app.asar`）逐字节复核后，**唯一未对齐的是长清单的窗口化折叠**：本仓现为「完成项恒收起（无论几项）」，ZCode 为「>6 项才折叠：取 3 项窗口 + 窗口前后各一条折叠行」。本票只补这一项。

## 目标算法（ZCode 实测，app.asar @315031925）

```
常量：阈值 6、窗口 3
若 项数 ≤ 6 → 不折叠，全量展示
锚点 = 第一个 in_progress；若无 → 第一个非 completed；再无 → 末尾
窗口起点 = clamp(锚点, 0, n-3)；窗口 = 连续 3 项
窗口前组、窗口后组各渲染一条折叠行（组为空则无折叠行）
折叠行文案：
  前组全为 completed → 「已完成 N 项」；否则 → 「前面 N 项」
  后组全为 pending   → 「待处理 N 项」；否则 → 「后面 N 项」
  「待处理」展开时文案变「收起 N 项待处理」
交互：折叠行是 button（aria-expanded）；悬浮弹出被折叠项（openDelay≈120ms / closeDelay 80ms，
提取时被截断，实现时现读核准）；点击/键盘切换固定；触摸设备（hover:none）点按；
同一时刻至多一组展开。
```

## 验收（AC）

1. **阈值**：清单 ≤6 项时不渲染任何折叠行、全量展示（含完成项，划线呈现）。**（对齐 ZCode；改变 #381 的「完成项恒收起」行为）**
2. **窗口**：>6 项时渲染 3 项窗口；锚点与窗口起点按上式，含边界：锚点 > n-3 时 clamp 到 n-3；无 in_progress、无未完成项的退化路径。
3. **折叠行文案**：按上式四分支 +「收起 N 项待处理」；空组不渲染折叠行。
4. **交互**：可聚焦 button + `aria-expanded`；click / Enter / Space 切换；悬浮打开（延迟开合）；触摸点按；互斥——打开一组自动收起另一组。
5. **黄金夹具**：17 项（顺序 = 11×completed, 1×pending, 1×in_progress, 4×pending）→「前面 12 项」+ 窗口 3 项（in_progress + 2×pending）+「待处理 2 项」；与用户实测 ZCode 截图同形（前面 12 项 / 待处理 2 项 / 11/17）。
6. **无回归**：#381 既有断言全保持——零项不渲染；当前项 activeForm 高亮 + → 前缀；双 in_progress 只高亮第一个；未知 status 按未完成渲染；整表更新时行节点身份存续（`key={item.id}` + `hidden` 保结构）。
7. **单一事实源**：仍只消费 `ConversationState.plan`（`task/plan_updated` 投影）；不新增前端清单状态（不变量 #22）。
8. **测试形态**：(a) 折叠决策抽为纯函数并单测（空 / 1 / 6 / 7 项 / 全完成 / 无 in_progress / 锚点近尾 clamp / 单组为空）；(b) 组件测试覆盖折叠行文案与 `aria-expanded`/互斥；(c) 1 条 focused e2e 覆盖悬浮弹出 + 点击固定（真实渲染）。

## 明确不做

- TUI 同步（只动 Web/桌面；TUI 保持 W-28 薄渲染 + ctrl+t）；
- 计数器文案改动（保持 #381 逐字「进程 N 项 · 已完成 M 项」，不对齐 ZCode 的 `M/N` 形式）；
- 「全完成变绿」等其它 ZCode 细节；
- 消息流内嵌待办卡片；
- 数据模型 / `task/plan_updated` / 投影 / `update_plan` 工具契约的任何改动；
- 不复制 ZCode 代码或资源（闭源，仅行为对齐）。

## 方案依据（SDD §1.3）

**来源（≥2 独立）**

1. **ZCode 桌面端本机 bundle**（闭源；2026-10-08 读取）：`D:\DevTools\ZCode\resources\app.asar` — `XCt`（折叠窗口算法 @315031925）、`$Ct`（折叠行文案四分支 + hover 弹出 @315031936）、`ZCt`（行渲染）、`ewt`（前/窗口/后三段 @315034644）、`kCt`（计数模型 @315020333）、i18n @297628915。**算法与文案的唯一权威。**
2. **opencode 官方仓库**（2026-10-08 读取）：`packages/tui/src/feature-plugins/sidebar/todo.tsx`（>2 条才出折叠、全部完成即隐藏）、`packages/app/src/pages/session/composer/session-todo-dock.tsx`（done/total + 折叠）——折叠/可见性阈值的独立收敛先例。
3. **Claude Code 官方文档**（2026-10-08 读取）：`code.claude.com/docs/en/interactive-mode.md`（Ctrl+T 清单视图、最多 5 条、折叠状态随 resume 恢复）、`agent-sdk/todo-tracking.md`（N/M 计数范式）。
4. **Codex CLI 本地克隆**（`D:\reference\codex` @7f89227）：`codex-rs/tui/src/history_cell/plans.rs`（`Updated Plan · x/y complete`；`ActivityDisclosure` + `DETAIL_PREVIEW_LINES = 3`）——「窗口取 3」在另一产品的同值先例。
5. **本仓既有**：`docs/research/2026-09-27-agent-progress-visualization-research.md`（渲染四件套收敛）+ PRD §7.5。

**机制摘要**：ZCode 的做法是「事件驱动的清单投影 + 呈现端纯函数窗口化」——数据端整表覆盖，呈现端按锚点取 3 项窗口、两端折成可展开行；折叠行文案由「被折叠组的构成」决定（全完成 → 已完成；全待办 → 待处理），因此**没有**独立的「完成项收起」规则。opencode / Claude Code / Codex 用不同阈值（>2 条 / ≤5 条 / 3 行预览）解决同一问题：长清单只留焦点窗口，是全行业收敛形态。

**契合点**：纯渲染层改动，不触碰不变量 #3（append-only 事件）与 #22（不维护第二套清单真相）；PRD §7.5「可折叠列表」字面已满足，本票把「折叠」升级为 ZCode 式窗口化折叠，改的是 #381 的渲染细节而非其契约；`_should_inject_plan`（模型上下文重注入）不受影响。

**判定**：**PORT DESIGN**（渲染行为自 ZCode 可观察行为移植）+ **BUILD**（实现自写；本仓 React 组件与 ZCode 无共享代码）。

**License**：ZCode 闭源 → 零代码 / 零资源复制，仅对齐可观察行为，文案为本仓自写中文；opencode / Codex / Cline 等为开源（MIT / Apache-2.0 系），本票**不实质复制任何第三方代码**，引用均为链接与行为描述。

## 夹具与测试

- 黄金夹具（AC5）进纯函数单测与组件测试；折叠行文案逐字断言；
- focused e2e：真实渲染下悬浮打开/关闭、点击固定、互斥；
- 落点时按 `docs/agents/verification.map.tsv` 核对 `web/src/components/` 所在 surface 行的 `lanes` / `focused` / `neg_tier` 是否仍成立。

## 风险 / 残余

1. ZCode 折叠行的弹出延迟常数提取时被截断（≈120/80ms）：实现时现读核准并在测试中钉值。
2. 本仓既有 popover 为点击语义；悬浮触发层需新增最小实现（portal / 定位复用既有原语）。
3. 若 #384（W-30 真实模型 Gate）暴露清单规模远超预期，窗口化在真机上的表现需复核（登记为残余，不阻塞本票）。
4. 视觉验收（与 ZCode 截图逐项对照）需真实清单会话，在 Runtime Verification 阶段用真实入口取证。

## 工作指令

按 SDD V3.1-lite 逐票主干执行：TDD（纯函数红→绿）→ focused 测试 + `tsc` / `oxlint` / `vitest` → 冻结树全量 → 两轴独立审查 → Gate-0 → 桌面同构核对（Electron 承载同一组件）。

## 参考资料

- 本机 ZCode bundle 提取脚本（只读）：系统临时目录，不入库；关键字节偏移见方案依据。
- 查重结论：`gh issue list` 全量检索「清单 / 折叠 / 窗口化」无重复票；W-26~W-29 已关单，#384（W-30）保持独立。
