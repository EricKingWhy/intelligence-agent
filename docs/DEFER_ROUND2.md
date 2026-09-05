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

---

# Round 3 细节加固 — 设计级 DEFER 清单

> 来源：Round 3 并行审计（MCP/memory writeback/skills discovery/context tokens/sandbox）。
> 同样按 AGENTS.md §5/§8 报告不改。

## R3-1. MCP 子系统整体未实现（spec gap，未来 phase）

**位置**：`src/agent_harness/mcp/` 目录不存在。

**现状**：spec 09 §1/§10 要求的 Remote MCP Server → Client → Adapter → Registry →
Executor 链路完全缺失；`pyproject.toml` 无 MCP 依赖。

**为何 DEFER**：按 14_IMPLEMENTATION_ROADMAP，MCP 属于未来 phase，当前不应超前
落地。任何 MCP 工具的统一执行路径问题（不变量 #7）、错误映射、连接生命周期
都没有对象可守——这是阶段问题，不是 bug。

**重启条件**：进入 MCP phase 时整体设计。

## R3-2. Memory writeback close() 取消而非 drain（设计决策）

**位置**：`src/agent_harness/memory/writeback.py::close`

**现状**：`close()` 对所有在飞任务 `task.cancel()`，不 drain。

**为何 DEFER**：当前契约已被
`tests/memory/test_context_provider.py::test_stream_mirrors_retrieval_degradation_and_writer_can_close`
明确钉死为"close 取消在飞任务"。改成 drain 是关停语义的设计选择
（快关 vs 不丢记忆），需要 Primary Developer 明确应用关停契约：
- 长 drain 会不会拖死滚动重启的 graceful shutdown budget？
- drain 超时后退回 cancel 的预算应是多少？

**重启条件**：用户/Primary 明确关停语义后改契约并改对应测试。

## R3-3. 超大 memory content 永久死信（设计）

**位置**：`src/agent_harness/memory/embeddings.py:14`（`check_embedding_ctx_length=False`）

**现状**：超过 embedding 模型 token 上限的 content 永远索引失败、5 轮后死信，
留在 SQLite 但永远 search 不到。

**为何 DEFER**：合理的修复要么在 store 入口截断（丢语义保可检索），要么在
embedding 前做 chunked embed（新能力）。两者都是设计决策。

**重启条件**：观察到真实生产记忆因超长被死信，或决定加 chunked embed 能力。

## R3-4. extractor 把整段会话无截断塞进单条 LLM prompt（设计）

**位置**：`src/agent_harness/memory/extractor.py:33-35`

**现状**：长会话 prompt 超模型上下文 → 静默回退到关键词启发式，无 degraded 事件
或日志区分。

**为何 DEFER**：修复需要在 extractor 内引入 token 预算 + 分段抽取 + 显式 degraded
事件，是 extractor 的新设计，超出当前 scope。

**重启条件**：profile 显示长会话下 LLM 抽取路径频繁回退到启发式。

## R3-5. 本地 sandbox 泄漏 host 环境变量（安全加固，需设计）

**位置**：`src/agent_harness/sandbox/local.py:121-131`（`Popen` 未传 `env=`）

**现状**：模型经 `sandbox.exec("env")` 能拿到 host 的 `OPENAI_API_KEY`、
`DATABASE_URL` 等任意环境变量——绕过文件系统路径安全（不变量 #11 边界泄漏）。

**为何 DEFER**：local.py 文档已声明是"开发/测试后端，不做进程级隔离"，
生产应走 docker.py。但即便开发后端，过滤 env 是有价值的硬化。延后是因为：
- 需要明确白名单（`PATH`、`SYSTEMROOT`、`LANG`、`HOME`、`USERPROFILE` 等）
  且不能破坏既有依赖 env 的命令测试。
- 这是安全决策，应向用户明示而非默写。

**建议**：未来引入 `SandboxEnvPolicy` 注入白名单/黑名单，默认拒绝凭据式键
（`*_KEY`/`*_TOKEN`/`*_SECRET`/`*_CREDENTIAL*`），其余按白名单。

**重启条件**：用户批准 sandbox 安全硬化，或观察到开发环境真实泄漏。

## R3-6. 本地 sandbox 无 FS 限制（已知限制，文档化）

**位置**：`src/agent_harness/sandbox/local.py`

**现状**：`cwd=workspace_root` 仅设初始工作目录，`shell=True` 下 `cd /`、
绝对路径都能逃出 workspace。

**为何 DEFER**：local.py 文档已显式声明不做进程级隔离，生产走 docker.py。
真正修复需要在 POSIX 引入 bwrap 类 namespace 封装——属于重大设计变更。

**重启条件**：决定给本地后端加 namespace 隔离能力。

## R3-7. context_provider MEMORY_DEGRADED 事件不带 run_id（跨层）

**位置**：`src/agent_harness/memory/context_provider.py:54-57`

**现状**：降级事件不带 `run_id`，无法归因到具体 run；writeback 路径却带了。

**为何 DEFER**：ContextProvider 在 build() 内被调用，不知道当前 run_id；
要带 run_id 得改 ContextProvider 协议（注入 run_id）或在 session 里跟踪
current run——跨层改动，超出加固 scope。

**重启条件**：观察到多 run 交错场景下降级事件归因断裂的真实痛点。

## R3-8. estimate_message_tokens 对多模态图像爆炸（无触发路径）

**位置**：`src/agent_harness/context/tokens.py:12-14`

**现状**：`model_dump_json()` 把 base64 image 也算进 token 数；单张 1MB 图
会让估计多出 ~330K token，触发过早压缩。

**为何 DEFER**：当前 codebase 无任何多模态入口（grep `image_url`/`image_url`
零命中）；按 §8 不为不存在的输入加防御。待真实多模态路径出现时再做内容
分类剔除（替换 base64 为固定图像开销常量）。

**重启条件**：引入多模态消息路径时。

---

# Round 4 细节加固 — 设计级 DEFER 清单

> 来源：Round 4 并行审计（model/provider/logging/config + session/storage/web）。

## R4-1. Model Fallback 链未实现（spec 02 §7，未来 phase）

**位置**：`src/agent_harness/model/provider.py`、`src/agent_harness/cli.py`

**现状**：spec 02 §7 明确要求 transient 失败（timeout/429/provider unavailable）
可触发 fallback 到备用 provider/model，并记录 primary/fallback + reason + attempt；
当前 `create_chat_model` 只返回单个 ChatOpenAI，无 fallback 链、无重试、无归因事件。

**为何 DEFER**：fallback 链是 ModelConfig + provider + runtime 协同的新设计，
属 spec 02 要求的功能但非当前 phase 必需；落地需要错误分类（transient vs
config vs auth）与 fallback 链 schema，应作为独立 ticket 由 Primary Developer
主导，不在加固循环里塞进来。

**重启条件**：进入 model fallback 的专门 ticket。

## R4-2. Session 同 id 多实例并发 append 可能重号（无真实触发路径）

**位置**：`src/agent_harness/session/session.py::append`

**现状**：`_next_seq` 是实例内存字段；同一 session_id 被两个 Session 实例
持有并发 append 会生成两条同 seq 行，`Session.resume` 严格校验会 brick。

**为何 DEFER**：当前所有调用路径都不并发持有同 id 的两个 Session 实例
（web 每次新建 session；recovery 单进程串行；test 都独立 store）。按 §9.1
"不存在的攻击面"，为不存在的并发路径加 store 层 seq 分配或文件锁属于投机。
真实修复（seq 分配下沉到 store 单一事实源，或文件级排他锁）是跨层改动。

**重启条件**：观察到真实生产出现同一 session 被并发持有两个实例的场景。

## R4-3. derive_messages 与 detect_dangling 用两套工具配对真相源（设计）

**位置**：`src/agent_harness/session/derive.py`

**现状**：derive_messages 基于 `MODEL_COMPLETED.tool_calls`；detect_dangling
基于独立 `TOOL_CALL` 事件。两条路径在当前 runtime 发射顺序下暂时一致，但
任何旁路写入（replay、手工拼装、未来不经过 Ledger 的工具路径）会让两端分歧。

**为何 DEFER**：统一两端是 derive 设计层面的重构（选哪个作单一真相源、
对现存所有事件流的影响），超出加固 scope。

**重启条件**：出现 derive_messages 投出孤立 ToolMessage 的真实事件流。

## R4-4. JSONL 写入 flush 无 fsync + 文本模式可能撕裂行（无多进程触发）

**位置**：`src/agent_harness/session/store.py`

**现状**：`fh.flush()` 不调 `os.fsync`；文本模式单逻辑 write 可能拆多次
syscall，多进程 append 可能交错损坏行。

**为何 DEFER**：单进程内文本 TextIOWrapper 缓冲不会撕裂；多进程并发 append
同一 session JSONL 当前不存在触发路径（见 R4-2）。引入 O_APPEND + os.write
单次 syscall + fsync 是性能与耐久性的权衡，需要专门设计。

**重启条件**：决定支持多进程并发写入同一 session，或要求断电级耐久性保证。

## R4-5. SqliteOperationLedger 每操作新连接、无 busy_timeout PRAGMA

**位置**：`src/agent_harness/storage/sqlite.py`

**现状**：`initialize` 只设了 `journal_mode=WAL`，无 `busy_timeout`/`synchronous`；
aiosqlite 默认 timeout=5s，长写并发会抛 `database is locked`。

**为何 DEFER**：当前恢复路径由文件级 pessimistic lock 串行化，写并发不激烈；
改 PRAGMA + 单长连接 + 事务边界是 ledger 性能/并发设计的专门工作，应与
Primary 协同。

**重启条件**：观察到真实 ledger 写并发超时，或进入 ledger 性能优化 ticket。

## R4-6. EVENT_TYPES 白名单"半强制"：formatter 静默改写为 system_log

**位置**：`src/agent_harness/logging.py:205-207`

**现状**：业务经 log_event 强校验；绕过 log_event 直接 `logger.log(...,
extra={"event_type": "...拼错..."})` 会被 formatter 静默归到 system_log，
无告警。

**为何 DEFER**：本仓当前所有 event_type 写入都经 log_event，无旁路；改 formatter
为告警模式属诊断协议变更，影响面需评估。

**重启条件**：出现真实旁路写入拼错 event_type 的案例。
