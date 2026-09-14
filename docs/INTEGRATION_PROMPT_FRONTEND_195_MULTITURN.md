# 集成提示词：#195 前端半（多轮投递：队列条 + 消息动作行 + supersede 编辑）

> 后端半契约冻结见 `docs/INTEGRATION_PROMPT_BACKEND_196_MULTITURN.md`（feat/backend）。
> 本单是前端半（feat/frontend，独立 clone `D:\intelligence-agent-frontend`）。

## 状态

- 后端：`feat/backend` `3d9dc28`（#196 消费侧闭环）+ `706fb2e`（review 修复）
- 前端：`feat/frontend` `3d3e591`（#195 前端半）+ `d20cc3c`（review 修复）+ tracker commit
- 两端均**未合入 main、未 push**；集成顺序：feat/backend → feat/frontend → main（§14.9）
- 关单：#196 / #195 均已关闭（comment 带验证证据）

## 交付内容（feat/frontend 3d3e591 + d20cc3c）

| 文件 | 变更 |
| --- | --- |
| `web/src/components/Composer.tsx` | `locked = approvalPending`（D10）；Enter=queue / Ctrl+Enter=steer / Shift+Enter 换行；IME composition 守卫；队列条（排队/引导徽标 + 编辑/立即/取消 + 「立即发送全部」= POST /queue/flush） |
| `web/src/components/Conversation.tsx` | 用户消息动作行（复制/编辑/分叉）；编辑仅最新一条可用（D8，置灰+title）；编辑态原地 textarea（Ctrl+Enter 保存 / Esc 取消）；被取代轮**问与答整段**不渲染（§4.5.1） |
| `web/src/hooks/useSession.ts` | 历史装载后 `GET /queue` 首屏补齐（替换语义）；`sendSteer` / `flushQueue`（409 轮询 3 次）/ `cancelItem`（404 幂等静默，其余失败上浮 error） |
| `web/src/App.tsx` | `handleEditTurn` → sendMessage 带 `supersedes_seq`；队列条四动作 + onFlush 接线 |
| `web/src/lib/projection.ts` | `supersedeRanges`（区间 [s,n) end 独占，与后端同判据）/ `applySupersedeShadow` / `projectUndelivered`（五事件逐事件折叠/摘除）/ `restoreUndeliveredFromQueue`；EVENT_SEMANTICS 六条新类型注册 |
| `web/src/lib/api.ts` | `supersedes_seq`/`queue_id` 请求字段；`QueueItem`/`SteerItem`/`SessionQueue` + `listSessionQueue`/`flushSessionQueue`/`cancelQueueItem` |
| `web/src/types.ts` | `ConversationState.undelivered` / `UndeliveredInput` / `Turn.superseded` |
| `web/src/generated/event-types.ts` | 从后端同步重新生成（`QUEUE_CONSUMED`/`MESSAGE_SUPERSEDED`） |
| `web/src/styles/app.css` | 队列条/消息动作行/编辑态样式（全部双主题语义 token，§15 无需双块） |
| `web/e2e/multiturn-queue.spec.ts` | 8 条：T11 编辑旧段消失（含回答段）+ supersedes_seq=seq / T11b 非最新置灰 / T12 queued JSON 不报错且空队列不渲染 / T12b 首屏补齐队列条 |
| `web/e2e/fixtures.ts` | queue / queue cancel 端点 mock（onQueueGet / onQueueCancelPost） |

## 契约要点（集成时核对）

1. **同构区间**：前端 `supersedeRanges` 与后端 `derive.superseded_ranges` 同一判据（只看 seq 与"是否也被取代"）；端点写法不同（前端 end 独占）但数学等价。
2. **事件流是唯一事实（D5）**：队列条数据来自事件流逐事件折叠 + `GET /queue` 只做首屏补齐（替换语义）；取消/投递摘除由 `queue/cancelled` / `queue/consumed` / `steer/applied` 事件驱动，前端不本地摘。
3. **D8 两端同判据**：后端 `_assert_supersedable` 与前端 `latestEditableTurn` 都排除 `injected_by` 消息（`706fb2e` 修复后一致）。
4. **D10 键位**：Enter=queue；Ctrl/Cmd+Enter=steer；Shift+Enter 换行；IME `isComposing` 守卫。
5. **§4.5.1**：被取代轮问与答整段从视图移除；Timeline 事件照旧（`message/superseded` 在 events 日志）。

## 门禁（集成后在 main 复跑）

- 后端：`ruff check src/ tests/` + `pytest -q`（2300 passed 基线）
- 前端：`cd web && npx tsc -b && npx vitest run && npx oxlint && npx playwright test --workers=2 && npx vite build`（832 unit / 334 e2e 基线，e2e 必须 `--workers=2`）

## 残余（不阻塞集成）

- T7（真实 provider 连续 steer 冒烟，人工项）：结论待回填 ADR-0030 §11
- Standards P3（未修，均有据）：multiturn_delivery 测试的 `sleep(0.3)` 时序猜测（_predicate 等待更稳）；`flushQueue` 先改 ref 后知结果（低影响）；TurnView 编辑态在虚拟化窗口复用时可能带旧值（罕见）；`restoreUndeliveredFromQueue` 的 seq 用 MAX_SAFE_INTEGER（显示顺序非到达顺序，仅队列条展示）
