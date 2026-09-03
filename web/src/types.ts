/** Shared types — mirror backend SessionEvent / AgentEvent shapes.
 * Source of truth: src/agent_harness/agent/types.py + session/event.py
 * Frontend never owns truth; it projects events into view-models.
 */

/** A single SSE frame from POST /api/sessions or durable event from GET events. */
export interface AgentEvent {
  type: string;
  data: Record<string, unknown>;
  seq: number | null;
  run_id: string | null;
  step_id: number | null;
}

/** Session summary from GET /api/sessions. */
export interface SessionSummary {
  session_id: string;
  event_count: number;
  first_event_time: string | null;
  last_event_time: string | null;
}

// ── Event type constants (mirror session/event.py) ──
export const EventType = {
  SESSION_STARTED: 'session/started',
  USER_MESSAGE: 'user/message',
  RUN_STARTED: 'run/started',
  RUN_COMPLETED: 'run/completed',
  RUN_FAILED: 'run/failed',
  MODEL_STARTED: 'model/started',
  MODEL_DELTA: 'model/delta',
  MODEL_COMPLETED: 'model/completed',
  TOOL_STARTED: 'tool/started',
  TOOL_COMPLETED: 'tool/completed',
} as const;

// ── View-models (projection output) ──

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
