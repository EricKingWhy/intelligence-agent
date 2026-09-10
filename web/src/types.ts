/** Shared types — mirror backend SessionEvent / AgentEvent shapes.
 * Source of truth: src/agent_harness/agent/types.py + session/event.py
 * Frontend never owns truth; it projects events into view-models.
 */

/**
 * Event type constants are GENERATED from session/event.py (single source) —
 * see web/src/generated/event-types.ts. Do not hand-edit values here.
 */
export { EventType, STREAM_ONLY_TYPES, type EventTypeValue } from './generated/event-types';

/** A single SSE frame from POST /api/sessions or durable event from GET events. */
export interface AgentEvent {
  type: string;
  data: Record<string, unknown>;
  seq: number | null;
  run_id: string | null;
  step_id: number | null;
  /** Durable-event timestamp (SessionEvent.time, present on GET /events history).
   *  SSE frames don't carry it yet — projection falls back to client clock. */
  time?: string;
  /** Present on historical events read from the store. */
  event_id?: string;
  /** Present on SSE-streamed events (injected by POST /api/sessions endpoint).
   *  Absent on historical events read from the store (session_id is known from the URL). */
  session_id?: string;
  /** Reasoning 块聚合键——envelope 顶层字段（T-contract #116，后端 SessionEvent.block_id
   *  序列化位置）。reasoning/started|delta|completed|interrupted 携带；data.block_id
   *  是 legacy 容错位（投影解析顺序：envelope → data → 合成）。 */
  block_id?: string;
}

/** Session summary from GET /api/sessions. */
export interface SessionSummary {
  session_id: string;
  event_count: number;
  first_event_time: string | null;
  last_event_time: string | null;
  /** 首条 user/message content（后端截断 128 字符；无则 null）——Session Rail
   *  标题零额外请求预填（后端 Gap 3）。events 扫描保留为 fallback。 */
  first_user_message: string | null;
  /** Langfuse trace id（后端 Gap 2）。Langfuse Phase 15 才接入，当前恒 null——
   *  UI 显示「未追踪」，属预期降级而非故障。 */
  trace_id: string | null;
  /** Langfuse trace 可点击 URL（契约 2d7f87a，ADR-0018 D7 延伸）：后端用官方
   *  get_trace_url(trace_id) 构造，含 host + project_id，前端零 URL 拼接。
   *  trace_id 与 trace_url 并列不互替：前者机器可读（Copy 命令），后者人类
   *  可点击（详情面板超链接）。未启用 Langfuse 两者都 null。 */
  trace_url: string | null;
}

/**
 * Token 用量形状——model/completed.data.usage 与 run/completed.data.usage_total
 * （后端 Gap 1）。AgentEvent.data 是宽松 Record<string, unknown>，此接口是
 * projection 边界窄化解析的契约文档：三字段必须全为有限数，否则整体按 null
 * 处理（UI 显示「—」，绝不部分伪造或补零）。
 */
export interface UsageStats {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
}

// ── View-models (projection output) ──

/** 空状态示例任务注入 Composer 的载荷（对象引用变化即触发注入）。 */
export interface PresetTask {
  text: string;
  id: number;
}

/** 会话呈现模式——conversation 永远属于 mode 指向的会话（编译期保证）。
 *
 * - idle：无选中会话（空状态）。
 * - live：正在流式创建/跟随的新会话；sessionId 为 null 表示 POST 已发出、
 *   首帧尚未确认（session_id 由 SSE 帧注入）。
 * - viewing：查看（历史重建）中的会话。
 *
 * 迁移规则：live → viewing 只发生在流结束/出错/取消时；viewing/live 之间切换
 * 由 selectSession 处理（切走即放弃当前流，幂等）。
 */
export type SessionMode =
  | { kind: 'idle' }
  | { kind: 'live'; sessionId: string | null }
  | { kind: 'viewing'; sessionId: string };

export interface ToolCall {
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  /** DSH 四态语义（冻结决策 "Unique Product Signatures" 第 69 行）：
   *  running（进行中）| success（成功）| failed（失败）| stopped（被中断，≠ error）。 */
  status: 'running' | 'success' | 'failed' | 'stopped';
  result?: unknown;
  /** da394a9 批：before/after 内嵌 "use inspect_artifact(<id>)" marker 时
   *  archived=true + artifactId——diff 内容已归档到 artifact，视图渲染占位态。 */
  diff?: {
    before: string;
    after: string;
    truncated: boolean;
    archived?: boolean;
    artifactId?: string;
  };
  started_at?: string;
  completed_at?: string;
  /**
   * Raw event payloads (Trace Density Raw tier — Brief "Raw = 原始事件 JSON").
   * Verbatim shallow copies of the projection source events (type/time/step_id/data);
   * never fabricated, never synthesized from parsed fields.
   */
  raw_call?: Record<string, unknown>;
  raw_result?: Record<string, unknown>;
  /** Artifact produced by this tool call when output overflows the inline limit.
   *  Set by artifact/created event (Phase 5). Inspector fetches via inspect_artifact. */
  artifact?: ArtifactRef;
  /** T3（#96）：流式输出缓冲（tool/output_delta 逐段累积；按事件序保 channel）。 */
  output?: ToolOutputChunk[];
}

/** T3（#96）：工具输出流块（契约 C2 tool/output_delta）。
 *  channel 保真（stdout/stderr 分色，视觉可合并）；相邻同通道 delta 由投影
 *  合并进尾块，数组规模有界。result 到达后 chunks 保留（流式内容不丢弃）
 *  ——渲染优先 chunks，缺失回退既有 result 路径（视图不双写）。 */
export interface ToolOutputChunk {
  channel: 'stdout' | 'stderr';
  text: string;
}

/** Large tool output offloaded to the ArtifactStore (Phase 5, spec 06 §15).
 *  The model only sees a summary + this ref; the full content lives in storage. */
export interface ArtifactRef {
  artifact_id: string;
  size: number;
  mime_type: string;
  source_tool: string;
}

export interface ModelSegment {
  /** Accumulated streamed text so far (from model/delta legacy / text/delta durable). */
  text: string;
  status: 'streaming' | 'done';
}

/** 推理块状态（T2 #95）——块生命周期与原语层的共享别名（reasoningCursor /
 *  disclosure 引用此处，单一来源）。 */
export type ReasoningStatus = 'streaming' | 'completed' | 'interrupted';

/** T2（#95）：推理/进度块（契约 C1 reasoning 事件族，S1 双来源共用一种块）。
 *  source=model = provider 思考（reasoning_content）；source=agent = agent 进度
 *  叙述。completed/interrupted 后不可变（spec 02 §8.1），迟到 delta 丢弃。
 *  visibility=internal 的事件永不投影为本块（spec 02 §15 硬边界）。 */
export interface ReasoningBlock {
  /** 稳定聚合键（data.block_id；缺失时投影合成 `r:{step}:{seq}`，重放确定）。 */
  blockId: string;
  source: 'model' | 'agent';
  /** 累积流文本（折叠前读视口与展开面消费同一份缓冲——两视图永不失同步）。 */
  text: string;
  status: ReasoningStatus;
  started_at?: string;
  completed_at?: string;
}

export interface Turn {
  step_id: number;
  /** User input that kicked off this turn. */
  user_message: string;
  /**
   * Latest model segment (kept for streaming caret + existing consumers).
   *
   * INVARIANT: `model === segments[latest model activity's index]` — the same
   * object, not a copy. applyEvent's clone breaks this alias (it clones model
   * and segments separately), so it re-aligns the reference after cloning;
   * mutations to one must stay visible through the other. If this field is
   * ever removed, the re-alignment step in applyEvent goes with it.
   */
  model: ModelSegment;
  /** All model segments in event order — one LLM burst each (execution chain). */
  segments: ModelSegment[];
  tools: ToolCall[];
  /** Execution chain in true event order: model bursts ↔ tool calls interleaved. */
  activities: TurnActivity[];
  status: 'streaming' | 'done' | 'failed';
  started_at?: string;
  completed_at?: string;
  /** Phase 12 白盒透明（ADR-0014 #69）：tool/failure-guard 落所在轮——
   *  soft 渲染为系统提示条，hard 渲染为终止标记。 */
  notices?: RunFailureGuard[];
  /** Phase 13 Multi-Agent（ADR-0015）：agent/delegation-started 创建节点、
   *  finished 按 child_session_id 回填——turn 级编排事实（与 notices 机制对齐，
   *  ConversationState 不加全局字段）。 */
  delegations?: Delegation[];
  /** Harness 注入纠正消息的来源标记（user/message data.injected_by）——
   *  非真人输入，渲染为系统提示条而非用户气泡。 */
  injected_by?: string;
  /** T9 #139：本轮 run 的轮次索引（1-based，来自 run/started data.turn_index）。
   *  per-turn 事实——同轮所有事件共享，供 TurnView 渲染「第 N 轮」标签。
   *  null = 该轮未携带该字段（旧版后端 / 非 run 起始路径）。 */
  turn_index: number | null;
  /** T2（#95）：reasoning 块字典（按 blockId 索引；顺序事实在 activities——
   *  reasoning 与 model/tool 是 S2 兄弟节点）。delta 高频更新走 COW 单块替换。 */
  reasoningById?: Record<string, ReasoningBlock>;
}

/** RepeatedToolFailureGuard 触发记录（tool/failure-guard 事件，ADR-0014 #69）。
 *  soft = 注入 user-role 纠正消息；hard = 终止本轮（end_run）。 */
export interface RunFailureGuard {
  level: 'soft' | 'hard';
  tool_name: string;
  consecutive_failures: number;
}

/** Multi-Agent 委派（agent/delegation-started / finished，Phase 13 ADR-0015）。
 *  父流白盒编排事实：child 的完整多轮历史在 child 自己的 session（后端 Gate 4
 *  不变量），父流只有 start/finish 两个锚点。阻塞语义：finished 返回即 child
 *  已终态。
 *
 *  status 说明：契约冻结 finished.status ∈ {completed, failed}（无中间态）。
 *  前端视图态在此基础上扩展两个——running（started 已到、finished 未回填）与
 *  stopped（父 run 中断时未回填的委派，finalizeRun settle；中断 ≠ 错误，与
 *  tool 同一 DSH 语义域）。stopped 不是契约值，只由前端投影产生。
 *
 *  copy-on-write 契约：delegations 数组与 tools 同规则——cloneTurn 会浅拷贝
 *  数组，但变更必须整体 reassign（或经 pushDelegation），禁止对克隆前共享的
 *  数组原地 push（会污染旧 turn 引用）。 */
export interface Delegation {
  /** 子代理 profile 名：'research_review' | 'coding'（V1 内置）。 */
  target: string;
  /** 给 child 的完整任务描述（自洽，父对话不含在内）。 */
  task: string;
  /** child 独立会话 id——钻取主键，UI 可见可复制。 */
  child_session_id: string;
  status: 'running' | 'completed' | 'failed' | 'stopped';
  /** 结构化结果的文字摘要（可能带后端 #86 溢出截断指针后缀）。 */
  summary?: string;
  started_at?: string;
  completed_at?: string;
}

/** One entry of a turn's execution chain, in true event order (Trace Ladder). */
export type TurnActivity =
  | { kind: 'model'; /** Index into turn.segments. */ index: number }
  | { kind: 'tool'; tool_call_id: string }
  | { kind: 'delegation'; child_session_id: string }
  | { kind: 'reasoning'; blockId: string };

/** Context compaction record (context/compacted event, Phase 5 spec 06).
 *  Run-level metadata — the Inspector Context panel surfaces these. */
export interface ContextCompaction {
  compacted_turn_count: number;
  summary_message_count: number;
  token_estimate: number;
  fallback_used: boolean;
  time?: string;
}

/** A tool operation that crashed mid-flight and needs human reconciliation
 *  (operation/reconcile-required event, Phase 4/5 spec 07 §13).
 *  Surfaces in the Inspector as an approval queue item. */
export interface ReconcileRequired {
  tool_call_id: string;
  tool_name: string;
  args_identity: string;
  state: string;
  time?: string;
}

/** Pending interactive approval — tool/approval-requested event (#37, PRD §2.2).
 *  Runtime pauses at ToolExecutor._check_approval until user resolves via
 *  POST /api/sessions/{id}/approve. ApprovalCard renders this inline. */
export interface PendingApproval {
  approval_id: string;
  tool_name: string;
  tool_call_id: string;
  action_type: string;
  title: string;
  description: string;
  arguments_preview: Record<string, unknown>;
  permission: string;
  policy: string;
  reason: string;
  allowed_decisions: string[];
  time?: string;
}

export interface ConversationState {
  session_id: string;
  turns: Turn[];
  /** The turn currently receiving events, if streaming. */
  active_step_id: number | null;
  run_status: 'idle' | 'running' | 'completed' | 'failed';
  /** run/failed.data.reason === 'cancelled'（客户端断连，df4f7d8→da394a9 批语义）
   *  时为 true——Run Pulse 显示「已取消」（中断 ≠ 错误，同 bash stopped 语义域）。 */
  run_cancelled: boolean;
  /** Run-level metadata for the Inspector (Phase 5 events). */
  compactions: ContextCompaction[];
  reconcile_queue: ReconcileRequired[];
  /** Pending interactive approvals (#37, PRD §2.2).
   *  tool/approval-requested adds to this list; permission/resolved removes.
   *  Empty array = no pending approval (auto-approve or already resolved). */
  pending_approvals: PendingApproval[];
  /** Every event that flowed through the projection, in arrival order (verbatim).
   *  Timeline tab truth source — never filtered or reshaped (invariant #22). */
  events: AgentEvent[];
  /** Events whose type didn't match any known case (UnknownSurfaceNode 协议,
   *  冻结决策第 69 行 "unknown 事件渲染为 raw 行兜底，永不静默丢弃")。
   *  Kept separately so Timeline / Inspector can surface them explicitly
   *  rather than dropping silently. Subset of `events`. */
  unknown_events: AgentEvent[];
  /** Run-level observability（后端 Gap 1/2）。全部来自事件真值，缺失即 null——
   *  UI 显示「—」/「未追踪」，绝不伪造 0：
   *  - model：最新携带 data.model 的 model/completed；
   *  - usage_total：run/completed.data.usage_total（权威聚合）覆盖前端对
   *    model/completed.usage 的累计值（运行中视图）；
   *  - cost_usd / trace_id / trace_url：run/completed 与 run/failed 对称携带
   *    （契约 2d7f87a——失败 run 也有可见 trace）；未启用 Langfuse 恒 null。 */
  model: string | null;
  usage_total: UsageStats | null;
  cost_usd: number | null;
  trace_id: string | null;
  trace_url: string | null;
  /** 最近一个携带 run_id 的事件的 run 归属（PRD §8.2 Inspector 头部 Run ID）。
   *  事件真值，缺失即 null——UI 隐藏该位，不回退 session_id 冒充（零伪造）。 */
  run_id: string | null;
  /** Phase 12 白盒透明（ADR-0014）：最近一次 model/fallback——模型卡「已切换」态。
   *  字段缺失（形状不完整）时不记录（零伪造）；后续 model 已切 to_model。 */
  model_fallback: { from_model: string; to_model: string; reason: string } | null;
  /** T8 #138：最近一次 run/interrupted 事件的信息。null = 无中断。
   *  来自事件真值，用于 UI 显示「上次运行在第 N 步中断」。 */
  run_interrupted: { step_id: number | null; interrupted_seq: number | null; reason: string } | null;
  /** T9 #139：当前 run 的轮次索引（1-based）。来自 RUN_STARTED.data.turn_index。
   *  null = 尚未收到 RUN_STARTED 或字段缺失。UI 可据此显示「第 N 轮」。 */
  turn_index: number | null;
  /** T1（#94）幂等簿记：本会话已应用的持久事件 seq 集合（spec 02 §6.1 at-least-once
   *  去重键）。append-only 共享日志纪律（同 events）：只增不改、跨快照共享引用、
   *  绝不整体替换。null-seq 帧不入册——ephemeral 流式信号（model/delta 等）
   *  按契约永不持久化也永不去重。 */
  seenSeqs: Set<number>;
}
