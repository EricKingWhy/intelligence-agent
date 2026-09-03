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
}

// ── View-models (projection output) ──

/** 空状态示例任务注入 Composer 的载荷（对象引用变化即触发注入）。 */
export interface PresetTask {
  text: string;
  id: number;
}

export interface ToolCall {
  tool_call_id: string;
  name: string;
  args: Record<string, unknown>;
  status: 'running' | 'success' | 'failed';
  result?: unknown;
  diff?: { before: string; after: string; truncated: boolean };
  started_at?: string;
  completed_at?: string;
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
  model: ModelSegment;
  tools: ToolCall[];
  status: 'streaming' | 'done' | 'failed';
  started_at?: string;
}

export interface ConversationState {
  session_id: string;
  turns: Turn[];
  /** The turn currently receiving events, if streaming. */
  active_step_id: number | null;
  run_status: 'idle' | 'running' | 'completed' | 'failed';
}
