# Round 2 细节加固 — 设计级 DEFER 清单

> 来源：Round 2 细节审计（context builder / checkpoint / recovery / tooling）。
> 按 AGENTS.md §5 与 §8，这些是设计级或投机性变更，超出当前 ticket scope，
> 只报告不改；落地需 Primary Developer 确认后再开 ticket。

## 1. ContextBuilder 的压缩摘要缓存（设计级）

**位置**：`src/agent_harness/context/builder.py::ContextBuilder.build`

**现状**：每次越过 `auto_compact_threshold` 都会 `ContextCompactor(...)` 重建并
调用模型 `ainvoke` 生成摘要。同一会话在恢复 + 多轮 run 中多次重建上下文时，
若历史投影未变会重复跑摘要。

**为何 DEFER**：
- 缓存失效边界非平凡：键若只取 prefix 消息计数，会漏掉任何前缀内容的修改；
  键若取内容哈希，则每次 build 都要做一次完整哈希（开销可能抵消节省）。
- 缓存状态引入"会话级可变状态"，与 ContextBuilder 当前无状态契约冲突，
  需要重新定义缓存存放点（builder 实例？session？session_meta？）与失效策略。
- 属于性能优化而非正确性修复——按 §9.2 Simplicity First 与 §8 Scope Lock，
  当前不投机构造抽象；先有性能证据（profile）再决策。

**重启条件**：profile 显示压缩摘要调用是 run 时长的主要贡献者，或观察到
同一前缀连续触发 ≥3 次摘要。

## 2. ContextBuilder 单 build 内多次 estimate_message_tokens（微优化）

**位置**：`src/agent_harness/context/builder.py::build`（行 42, 64, 75）

**现状**：一次 build 内对同一组消息多次估算 token。

**为何 DEFER**：估算是纯函数、近 O(n)；合并需要在压缩前后区分估算点，
可读性代价大于收益。`estimate_message_tokens` 不是热路径瓶颈。

## 3. ContextBuilder 的 ContextProvider 去重（设计级）

**位置**：`src/agent_harness/context/builder.py::_with_providers`

**现状**：注入重复的 ContextProvider 时，两者贡献会叠加进入上下文。

**为何 DEFER**：
- 去重需假定"什么算重复"——按类型？按贡献内容哈希？这是装配者的意图，
  不是 builder 应替用户决策的事。
- 用 ContextBuilder 的人在构造时控制 `context_providers` 列表，去重职责
  更适合放在装配点（DI / capability wiring），而非投影入口。

**重启条件**：观察到真实装配路径会无意注入重复 provider 且无法在装配点修复。

## 4. OperationLedger 主键 = tool_call_id 单列（跨会话碰撞隐患）

**位置**：`src/agent_harness/storage/operation_ledger.py`

**现状**：Operation 以 `tool_call_id` 为主键，未含 session_id。跨会话复用同一
`tool_call_id`（例如测试固定 ID、或模型生成碰撞）会让两条 Operation 互相覆盖。

**为何 DEFER**：
- 改主键为 `(tool_call_id, session_id)` 复合键是 schema 迁移，影响
  list_for_session / get / update_state 全部查询路径与 #29/#30 已冻结契约，
  必须由 Primary Developer 走 SDD 流程评估。
- 现网风险低：模型生成的 `tool_call_id` 是高熵随机串；测试固定 ID 是测试
  范畴问题，不应驱动生产 schema。

**重启条件**：观察到生产 Ledger 出现 tool_call_id 碰撞导致的 Operation 覆盖。

## 5. 当前运行期的 tool/call + tool/result 块不可压缩（设计级）

**位置**：`src/agent_harness/context/compactor.py::_validate_tool_blocks`

**现状**：压缩算法把"完整 AIMessage + ToolMessage 块"视为原子单元；若单个
块本身就超预算，会直接 raise ContextWindowExceededError，没有更细的降级。

**为何 DEFER**：
- 这是 compactor 的有意设计：拆解 tool 块会让 AIMessage.tool_calls 与
  配对的 ToolMessage 失去对应关系，破坏不变量 #7（统一工具路径 + 配对语义）。
- 子块压缩（如把 ToolMessage.content 替换为引用 + artifact）属于专项设计，
  需与 ArtifactOverflowHandler / spec 06 共同演进。
