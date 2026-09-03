# intelligence-agent

一个 **Python / Async-first、轻量、透明、可恢复、插件化** 的通用 Agent Harness。
Coding / Knowledge / Research 等能力通过 Capability / Provider / Tool 可插拔接入，不是 Core 的特判分支。
本文件是项目的领域词汇表，只定义概念，不写实现细节。

## Language

**Sandbox**:
模型发起的命令与文件操作实际运行的隔离执行环境，是 Runtime 的安全边界而非 Prompt 约束。
_Avoid_: container, executor, environment

**Workspace**:
Sandbox 内部允许 Coding Tool 读写的唯一目录；越界访问会被 Sandbox 拒绝。
_Avoid_: working dir, project folder, bind mount, volume

**Coding Tool**:
在 Sandbox 内执行、按 `Tool` 契约暴露给模型的工具（read / write / edit / bash）。
_Avoid_: code tool, file tool, action

**Host**:
用户真实机器，Sandbox 的隔离边界之外的环境。
_Avoid_: 本机, server, node

**Container**:
由 Docker 提供的隔离执行环境，是 Sandbox 的一种后端实现。
_Avoid_: VM, jail, pod

**ExecResult**:
Sandbox 执行一条 shell 命令后返回的原生结果（exit_code / stdout / stderr / duration），由 bash 工具映射成 `ToolResult`。
_Avoid_: CommandResult, ShellResult, RunOutput

**命令业务失败**:
shell 命令返回非零 exit_code 但 Sandbox 调用本身成功（如 pytest 测试不通过）。这不是 Tool Runtime 异常，Agent 应读 stdout/stderr 决定下一步。
_Avoid_: tool failure, execution error, transient error

## Session / Event 层

**Session**:
一次会话的聚合根，持有 `session_id` 与已加载的事件列表；对外提供 `start` / `resume` / `append` / `derive_messages` / `begin_run` / `end_run` 等业务方法，是 Runtime 与外部世界（CLI / Web UI）交互的单一入口。
_Avoid_: conversation, dialogue, chat, context window

**SessionEvent**:
持久化、append-only、类型化的会话事实事件（`session/started`、`run/completed`、`tool/call` 等）。是 Resume / Replay / Fork / derive_messages 的唯一事实源；不可原地修改，修订用新事件表达。
_Avoid_: log entry, message, record, history item

**SessionStore**:
SessionEvent 的薄 IO 层，只负责"读 JSONL / append JSONL"两件事，不持有业务状态。V1 只实现 `JsonlSessionStore`，未来 SQLite / PostgreSQL 作为可替换后端接入。
_Avoid_: database, repository, event bus, message queue

**derive_messages**:
从 SessionEvent 序列投影出模型可见 messages 列表的纯函数。负责 tool_call / tool_result 配对与 dangling 检测；不修改事件、不产生副作用。遵循 `events → derive_messages() → message history → ContextBuilder` 流向。
_Avoid_: serialize messages, flatten history, get messages

**dangling tool_call**:
事件序列中存在 `tool/call` 但无匹配 `tool/result` 的孤立状态（通常因进程崩溃）。处理方式：检测后注入合成 `ToolMessage`（content 写明"执行被中断，结果未知"），让消息链自洽，模型能看到失败并自主决定下一步。
_Avoid_: orphan call, broken chain, missing result

**Run**:
一次 `AgentRuntime.run()` 调用的生命周期单元，绑定 `run_id`。同一 Session 可有多次 Run；Run 边界由 `run/started` 与 `run/completed` / `run/failed` 事件标记，是 Phase 14 Fork 的切分依据。
_Avoid_: turn, iteration, loop, attempt

**Resume**:
从已持久化 SessionEvent 加载 Session 并继续对话的能力。流程：`load events → validate seq → restore state → reconcile → continue`。Resume MUST NOT 默认重放已完成 Tool。
_Avoid_: restart, reload, reconnect, replay（Replay 是独立概念，见 Phase 14）

**Diagnostic Log**:
用于 debug / 性能追踪 / 全链路观察的结构化日志（span / trace / agent_decision / retry 等），写入 `logs/agent.jsonl`。与 SessionEvent 分层（不变量 #5：Event ≠ Log），不是业务事实源，不可用于恢复。
_Avoid_: event log, session log, audit trail
