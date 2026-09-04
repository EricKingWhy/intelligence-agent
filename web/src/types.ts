/** Shared types — mirror backend SessionEvent / AgentEvent shapes.
 * Source of truth: src/agent_harness/agent/types.py + session/event.py
 * Frontend never owns truth; it projects events into view-models.
 */

/**
 * Event type constants are GENERATED from session/event.py (single source) —
 * see web/src/generated/event-types.ts. Do not hand-edit values here.
 */
export { EventType, STREAM_ONLY_TYPES } from './generated/event-types';

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
  diff?: { before: string; after: string; truncated: boolean };
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
  /** Accumulated streamed text so far (from model/delta). */
  text: string;
  status: 'streaming' | 'done';
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
}

/** One entry of a turn's execution chain, in true event order (Trace Ladder). */
export type TurnActivity =
  | { kind: 'model'; /** Index into turn.segments. */ index: number }
  | { kind: 'tool'; tool_call_id: string };

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

export interface ConversationState {
  session_id: string;
  turns: Turn[];
  /** The turn currently receiving events, if streaming. */
  active_step_id: number | null;
  run_status: 'idle' | 'running' | 'completed' | 'failed';
  /** Run-level metadata for the Inspector (Phase 5 events). */
  compactions: ContextCompaction[];
  reconcile_queue: ReconcileRequired[];
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
   *  - cost_usd / trace_id：run/completed 携带（费率表未定义/Langfuse 未接入，
   *    当前恒 null）。 */
  model: string | null;
  usage_total: UsageStats | null;
  cost_usd: number | null;
  trace_id: string | null;
}
