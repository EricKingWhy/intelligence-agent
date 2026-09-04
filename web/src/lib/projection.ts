/** Event projection — pure functions turning AgentEvents into ConversationState.
 *
 * This is the ONLY place where events get interpreted into view-models.
 * No component mutates state directly; they all dispatch events here.
 * This satisfies invariant #22 (Web UI maintains no second truth) —
 * the events ARE the truth, this just projects them.
 */

import type { AgentEvent, ConversationState, ModelSegment, ToolCall, Turn, UsageStats } from '../types';
import { EventType } from '../types';

export function initConversation(session_id: string): ConversationState {
  return {
    session_id,
    turns: [],
    active_step_id: null,
    run_status: 'idle',
    compactions: [],
    reconcile_queue: [],
    events: [],
    unknown_events: [],
    model: null,
    usage_total: null,
    cost_usd: null,
    trace_id: null,
  };
}

function findOrCreateTurn(state: ConversationState, step_id: number): Turn {
  let turn = state.turns.find((t) => t.step_id === step_id);
  if (!turn) {
    turn = {
      step_id,
      user_message: '',
      model: { text: '', status: 'streaming' },
      segments: [],
      tools: [],
      activities: [],
      status: 'streaming',
    };
    state.turns.push(turn);
  }
  return turn;
}

/** Apply one event to state, mutating a draft. Call inside immer-style updater. */
export function applyEvent(state: ConversationState, event: AgentEvent): ConversationState {
  // Shallow-clone top-level for React. Components read nested fields by reference;
  // we mutate the clone's nested structures in place where noted.
  const next: ConversationState = {
    ...state,
    turns: state.turns.map((t) => {
      const turn: Turn = {
        ...t,
        model: { ...t.model },
        segments: t.segments.map((s) => ({ ...s })),
        tools: [...t.tools],
        activities: [...t.activities],
      };
      // model 与 segments[latest model index] 是同一逻辑段：clone 会切断引用，
      // 这里按 activities 记录的 index 重新对齐，保证后续 mutation 同步。
      const lastModel = [...turn.activities].reverse().find((a) => a.kind === 'model');
      if (lastModel && lastModel.kind === 'model') {
        turn.segments[lastModel.index] = turn.model;
      }
      return turn;
    }),
    compactions: [...state.compactions],
    reconcile_queue: [...state.reconcile_queue],
    // Inspector Timeline 真相源：流经的每个事件原样保留（不含 model/delta 折叠）
    events: [...state.events, event],
    unknown_events: state.unknown_events,
  };

  const { type, data } = event;

  switch (type) {
    case EventType.USER_MESSAGE: {
      const step = resolveStep(event, next);
      const turn = findOrCreateTurn(next, step);
      turn.user_message = String(data.content ?? '');
      break;
    }

    case EventType.RUN_STARTED: {
      next.run_status = 'running';
      break;
    }

    case EventType.MODEL_STARTED: {
      const step = resolveStep(event, next);
      next.active_step_id = step;
      const turn = findOrCreateTurn(next, step);
      touchTurn(turn, event);
      // 新 burst → 新段；空段（重复 MODEL_STARTED、尚无 delta）复用不追加
      const lastActivity = turn.activities[turn.activities.length - 1];
      const isEmptyHead =
        lastActivity?.kind === 'model' &&
        turn.model.text === '' &&
        turn.model.status === 'streaming';
      if (!isEmptyHead) {
        turn.model = { text: '', status: 'streaming' };
        turn.segments.push(turn.model);
        turn.activities.push({ kind: 'model', index: turn.segments.length - 1 });
      }
      turn.status = 'streaming';
      break;
    }

    case EventType.MODEL_DELTA: {
      const step = resolveStep(event, next);
      const turn = findOrCreateTurn(next, step);
      touchTurn(turn, event);
      turn.model.text += String(data.delta ?? '');
      turn.model.status = 'streaming';
      break;
    }

    case EventType.MODEL_COMPLETED: {
      const step = resolveStep(event, next);
      const turn = findOrCreateTurn(next, step);
      // Final content may include consolidated text — prefer it over accumulated delta.
      turn.model.text = String(data.content ?? turn.model.text);
      turn.model.status = 'done';
      // Run-level observability（后端 Gap 1）：可选字段，缺失/畸形不伪造。
      if (typeof data.model === 'string' && data.model) next.model = data.model;
      const usage = parseUsage(data.usage);
      if (usage) {
        // run/completed 权威聚合到达前，累计各次推理 usage 作为运行中视图。
        next.usage_total = next.usage_total
          ? {
              prompt_tokens: next.usage_total.prompt_tokens + usage.prompt_tokens,
              completion_tokens: next.usage_total.completion_tokens + usage.completion_tokens,
              total_tokens: next.usage_total.total_tokens + usage.total_tokens,
            }
          : usage;
      }
      break;
    }

    case EventType.TOOL_CALL: {
      const step = resolveStep(event, next);
      const turn = findOrCreateTurn(next, step);
      touchTurn(turn, event);
      const id = String(data.tool_call_id ?? '');
      if (!turn.tools.find((t) => t.tool_call_id === id)) {
        turn.tools.push({
          tool_call_id: id,
          name: String(data.tool_name ?? 'unknown'),
          args: (data.args as Record<string, unknown>) ?? {},
          status: 'running',
          // Raw 档真相源：完整源事件原样透传（type/time/step_id/data，Trace Density Raw）
          raw_call: { ...event },
          // 事件真值时间优先（历史事件带 time）；SSE 帧无 time 时回退客户端时钟
          started_at: event.time ?? new Date().toISOString(),
        });
        turn.activities.push({ kind: 'tool', tool_call_id: id });
      }
      break;
    }

    case EventType.TOOL_RESULT: {
      const step = resolveStep(event, next);
      const turn = findOrCreateTurn(next, step);
      const id = String(data.tool_call_id ?? '');
      const tool = turn.tools.find((t) => t.tool_call_id === id);
      if (tool) {
        // Backend serializes the full ToolResult via model_dump_json() — so content is
        // a JSON string shaped {ok, message, data, error_code, retryable, metadata, ...}.
        // The structured payload (incl. diff for edit/write) lives under `.data`.
        const parsed = tryParseContent(data.content);
        const ok = parsed?.ok === true;
        tool.status = ok ? 'success' : 'failed';
        const parsedData = (parsed?.data ?? null) as Record<string, unknown> | null;
        tool.result = parsedData ?? parsed?.message ?? data.content;
        // Backend edit/write/apply_patch tools spread diff fields (before/after/truncated)
        // directly into ToolResult.data — not nested under data.diff. Detect them here.
        if (
          parsedData &&
          typeof parsedData.before === 'string' &&
          typeof parsedData.after === 'string'
        ) {
          tool.diff = {
            before: parsedData.before,
            after: parsedData.after,
            truncated: parsedData.truncated === true,
          };
        }
        tool.completed_at = event.time ?? new Date().toISOString();
        tool.raw_result = { ...event };
      }
      break;
    }

    case EventType.RUN_COMPLETED:
      // 权威聚合（后端 Gap 1/2）：事件携带的 usage_total 覆盖前端累计值；
      // cost_usd / trace_id 缺失或 null 保持 null（费率表未定义 / Langfuse 未接入）。
      next.usage_total = parseUsage(data.usage_total) ?? next.usage_total;
      next.cost_usd = typeof data.cost_usd === 'number' && Number.isFinite(data.cost_usd) ? data.cost_usd : null;
      next.trace_id = typeof data.trace_id === 'string' && data.trace_id ? data.trace_id : null;
      finalizeRun(next, 'completed', event.time);
      break;

    case EventType.RUN_FAILED:
      finalizeRun(next, 'failed', event.time);
      break;

    case EventType.ARTIFACT_CREATED: {
      // Large tool output offloaded to ArtifactStore (Phase 5, spec 06 §15).
      // Attach the ref to the producing tool call so the Inspector can fetch it.
      const toolCallId = String(data.tool_call_id ?? '');
      const turn = next.turns.find((t) =>
        t.tools.some((tc) => tc.tool_call_id === toolCallId),
      );
      const tool = turn?.tools.find((tc) => tc.tool_call_id === toolCallId);
      if (tool) {
        tool.artifact = {
          artifact_id: String(data.artifact_id ?? ''),
          size: Number(data.size ?? 0),
          mime_type: String(data.mime_type ?? 'application/octet-stream'),
          source_tool: String(data.source_tool ?? ''),
        };
      }
      break;
    }

    case EventType.CONTEXT_COMPACTED: {
      // Context window exceeded → older turns summarized (Phase 5, spec 06).
      // Run-level metadata for the Inspector Context panel.
      next.compactions.push({
        compacted_turn_count: Number(data.compacted_turn_count ?? 0),
        summary_message_count: Number(data.summary_message_count ?? 0),
        token_estimate: Number(data.token_estimate ?? 0),
        fallback_used: data.fallback_used === true,
        time: event.time,
      });
      break;
    }

    case EventType.OPERATION_RECONCILE_REQUIRED: {
      // A tool operation crashed mid-flight and needs human裁决 (Phase 4/5, spec 07 §13).
      // Surfaces in the Inspector as an approval queue item.
      next.reconcile_queue.push({
        tool_call_id: String(data.tool_call_id ?? ''),
        tool_name: String(data.tool_name ?? ''),
        args_identity: String(data.args_identity ?? ''),
        state: String(data.state ?? 'NEED_RECONCILE'),
        time: event.time,
      });
      break;
    }

    // 会话生命周期事件：不投影到轮次/工具，但识别为已知事件（不进 unknown_events）。
    // Run Pulse 可消费 conversation.events 中的会话事件判断 resumed 等场景。
    case EventType.SESSION_STARTED:
    case EventType.SESSION_RESUMED:
      break;

    default:
      // UnknownSurfaceNode 兜底协议（冻结决策第 69 行）：未知事件类型不静默丢弃，
      // 记录到 unknown_events 供 Timeline / Inspector 显式渲染为 raw 行。
      next.unknown_events = [...next.unknown_events, event];
      break;
  }

  return next;
}

/** Resolve which step an event belongs to.
 *
 * Priority: explicit `data.step` → event `step_id` → active step → next turn index.
 * All six event cases used to inline their own variant of this chain; centralizing
 * it means a new event type can't accidentally pick a different fallback. */
function resolveStep(event: AgentEvent, state: ConversationState): number {
  const fromData = (event.data.step as number | undefined) ?? null;
  if (fromData !== null) return fromData;
  if (event.step_id !== null) return event.step_id;
  if (state.active_step_id !== null) return state.active_step_id;
  return state.turns.length + 1;
}

/** Mark a run as finished and settle every in-flight turn.
 *
 * RUN_COMPLETED and RUN_FAILED share the same sweep — only the terminal
 * turn.status differs ('done' vs 'failed'). Splitting them was the source of
 * a past caret-never-stops bug; the shared helper makes the invariant
 * "run ends → no streaming turn" structural. */
function finalizeRun(state: ConversationState, status: 'completed' | 'failed', time?: string): void {
  state.run_status = status;
  state.active_step_id = null;
  const turnStatus = status === 'failed' ? 'failed' : 'done';
  for (const turn of state.turns) {
    if (turn.status === 'streaming') turn.status = turnStatus;
    // run 终止后不再有 delta——所有段必须离开 streaming，否则 caret 永闪。
    // turn.model 与 segments[latest model index] 是同一对象（clone 后已对齐），
    // 因此遍历 segments 即同时覆盖 turn.model。
    for (const seg of turn.segments) {
      if (seg.status === 'streaming') seg.status = 'done';
    }
    // DSH 四态语义（冻结决策第 69 行 "interrupted ≠ error"）：run 结束时仍在 running
    // 的工具被中断而非失败——标记 stopped，不与 success/failed 混淆。
    for (const tool of turn.tools) {
      if (tool.status === 'running') tool.status = 'stopped';
    }
    if (turn.completed_at === undefined) {
      turn.completed_at = time ?? new Date().toISOString();
    }
  }
}

/** First touch of a turn records its true start time (event time preferred). */
function touchTurn(turn: Turn, event: AgentEvent): void {
  if (turn.started_at === undefined) {
    turn.started_at = event.time ?? new Date().toISOString();
  }
}

/** Expand a turn's execution chain into render nodes in true event order
 *  (Trace Ladder — signature #2). Pure view over `activities` — no filtering
 *  or collapsing; empty/done segments are a rendering concern (Conversation). */
export type ChainNode =
  | { kind: 'model'; segment: ModelSegment }
  | { kind: 'tool'; tool: ToolCall };

export function deriveChain(turn: Turn): ChainNode[] {
  return turn.activities.flatMap((a): ChainNode[] => {
    if (a.kind === 'model') {
      const segment = turn.segments[a.index];
      return segment ? [{ kind: 'model', segment }] : [];
    }
    const tool = turn.tools.find((t) => t.tool_call_id === a.tool_call_id);
    return tool ? [{ kind: 'tool', tool }] : [];
  });
}

/** Rebuild full conversation from a history of durable events (on page load). */
export function projectHistory(session_id: string, events: AgentEvent[]): ConversationState {
  return events.reduce(applyEvent, initConversation(session_id));
}

/** Extract a session-title string from a single event if it's a user/message
 *  with non-empty content; '' otherwise. Shared by deriveSessionTitle (history
 *  replay) and the live SSE handler so there's one definition of "title-worthy". */
export function extractSessionTitle(event: AgentEvent): string {
  if (event.type !== EventType.USER_MESSAGE) return '';
  return String(event.data.content ?? '').trim();
}

/** Extract the first user/message content from an event stream — used as the
 *  Session Rail row title (frozen decision "Session Model E 轮" 第 73 行:
 *  "会话行 = 首条用户消息截断为标题 + 短 ID + 事件数 + 相对时间 + Run Pulse 状态点"）。
 *  Empty / no-user-message-yet → returns '' (caller falls back to short ID). */
export function deriveSessionTitle(events: AgentEvent[]): string {
  for (const e of events) {
    const title = extractSessionTitle(e);
    if (title) return title;
  }
  return '';
}

/** Try to JSON-parse a tool result `content` string; return null on failure.
 * Backend serializes the full ToolResult via model_dump_json(), so this is the
 * only way to get at ok / data / error_code without re-fetching from the API.
 */
function tryParseContent(content: unknown): Record<string, unknown> | null {
  if (typeof content !== 'string') return null;
  try {
    const parsed = JSON.parse(content);
    return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

/**
 * 窄化解析 usage 形状（后端 Gap 1 契约，见 types.ts UsageStats）：三字段必须
 * 全为有限数，否则返回 null——缺失/畸形整体按「—」处理，绝不部分伪造或补零。
 */
function parseUsage(value: unknown): UsageStats | null {
  if (typeof value !== 'object' || value === null) return null;
  const v = value as Record<string, unknown>;
  const { prompt_tokens, completion_tokens, total_tokens } = v;
  const ok =
    typeof prompt_tokens === 'number' &&
    typeof completion_tokens === 'number' &&
    typeof total_tokens === 'number' &&
    Number.isFinite(prompt_tokens) &&
    Number.isFinite(completion_tokens) &&
    Number.isFinite(total_tokens);
  return ok ? { prompt_tokens, completion_tokens, total_tokens } : null;
}

/**
 * 单行事件摘要——projection 是事件→人类可读语义的唯一归属（不变量 #22）。
 * 被 Timeline / Trace / Chat 多处复用；未知事件走 UnknownSurfaceNode 兜底（冻结决策 69）。
 */
export function summarizeEvent(event: AgentEvent): string {
  const d = event.data;
  switch (event.type) {
    case EventType.USER_MESSAGE:
      return String(d.content ?? '').slice(0, 40);
    case EventType.MODEL_DELTA:
      return `+${String(d.delta ?? '').length} 字符`;
    case EventType.MODEL_COMPLETED: {
      // 后端 Gap 1：观测字段存在时优先展示（模型 · tokens）；否则回退内容长度。
      // 千分位等 locale 格式化归展示层（StepDetail），projection 保持确定性。
      const usage = parseUsage(d.usage);
      const model = typeof d.model === 'string' && d.model ? d.model : null;
      if (model || usage) {
        return [model, usage ? `${usage.total_tokens} tok` : null]
          .filter((p): p is string => p !== null)
          .join(' · ');
      }
      return `${String(d.content ?? '').length} 字符`;
    }
    case EventType.TOOL_CALL:
      return `${String(d.tool_name ?? '?')} ${JSON.stringify(d.args ?? {}).slice(0, 40)}`;
    case EventType.TOOL_RESULT: {
      const parsed = tryParseContent(d.content);
      if (parsed) {
        return parsed.ok === true ? 'ok' : `失败 ${String(parsed.error_code ?? '')}`;
      }
      return String(d.content ?? '').slice(0, 40);
    }
    case EventType.ARTIFACT_CREATED:
      return String(d.artifact_id ?? '').slice(0, 20);
    case EventType.CONTEXT_COMPACTED:
      return `${d.compacted_turn_count ?? '?'} 轮 · ${d.token_estimate ?? '?'} tok`;
    case EventType.OPERATION_RECONCILE_REQUIRED:
      return String(d.tool_name ?? '');
    case EventType.RUN_COMPLETED: {
      // 后端 Gap 1：聚合用量/成本（缺失字段不出现，全空则空摘要——类型标签已足够）。
      const usage = parseUsage(d.usage_total);
      const parts = [
        usage ? `${usage.total_tokens} tok` : null,
        typeof d.cost_usd === 'number' && Number.isFinite(d.cost_usd) ? `$${d.cost_usd}` : null,
      ].filter((p): p is string => p !== null);
      return parts.join(' · ');
    }
    // 已知生命周期事件无单行语义——空摘要，类型标签已足够。
    // 「未知事件」兜底必须只留给真正未知的类型（UnknownSurfaceNode 协议）。
    case EventType.RUN_STARTED:
    case EventType.RUN_FAILED:
    case EventType.SESSION_STARTED:
    case EventType.SESSION_RESUMED:
    case EventType.MODEL_STARTED:
    case EventType.MODEL_FAILED:
      return '';
    default:
      return `未知事件 · ${JSON.stringify(d).slice(0, 40)}`;
  }
}
