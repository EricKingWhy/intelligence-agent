/** Event projection — pure functions turning AgentEvents into ConversationState.
 *
 * This is the ONLY place where events get interpreted into view-models.
 * No component mutates state directly; they all dispatch events here.
 * This satisfies invariant #22 (Web UI maintains no second truth) —
 * the events ARE the truth, this just projects them.
 */

import type { AgentEvent, ConversationState, Turn } from '../types';
import { EventType } from '../types';

export function initConversation(session_id: string): ConversationState {
  return {
    session_id,
    turns: [],
    active_step_id: null,
    run_status: 'idle',
    compactions: [],
    reconcile_queue: [],
  };
}

function findOrCreateTurn(state: ConversationState, step_id: number): Turn {
  let turn = state.turns.find((t) => t.step_id === step_id);
  if (!turn) {
    turn = {
      step_id,
      user_message: '',
      model: { text: '', status: 'streaming' },
      tools: [],
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
    turns: state.turns.map((t) => ({ ...t, model: { ...t.model }, tools: [...t.tools] })),
    compactions: [...state.compactions],
    reconcile_queue: [...state.reconcile_queue],
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
      turn.model = { text: '', status: 'streaming' };
      turn.status = 'streaming';
      break;
    }

    case EventType.MODEL_DELTA: {
      const step = resolveStep(event, next);
      const turn = findOrCreateTurn(next, step);
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
      break;
    }

    case EventType.TOOL_CALL: {
      const step = resolveStep(event, next);
      const turn = findOrCreateTurn(next, step);
      const id = String(data.tool_call_id ?? '');
      if (!turn.tools.find((t) => t.tool_call_id === id)) {
        turn.tools.push({
          tool_call_id: id,
          name: String(data.tool_name ?? 'unknown'),
          args: (data.args as Record<string, unknown>) ?? {},
          status: 'running',
          // 事件真值时间优先（历史事件带 time）；SSE 帧无 time 时回退客户端时钟
          started_at: event.time ?? new Date().toISOString(),
        });
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
      }
      break;
    }

    case EventType.RUN_COMPLETED:
      finalizeRun(next, 'completed');
      break;

    case EventType.RUN_FAILED:
      finalizeRun(next, 'failed');
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

    default:
      // Unknown event types are ignored — forward-compatible.
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
function finalizeRun(state: ConversationState, status: 'completed' | 'failed'): void {
  state.run_status = status;
  state.active_step_id = null;
  const turnStatus = status === 'failed' ? 'failed' : 'done';
  for (const turn of state.turns) {
    if (turn.status === 'streaming') turn.status = turnStatus;
    // run 终止后不再有 delta——model 段必须离开 streaming，否则 caret 永闪
    if (turn.model.status === 'streaming') turn.model.status = 'done';
  }
}

/** Rebuild full conversation from a history of durable events (on page load). */
export function projectHistory(session_id: string, events: AgentEvent[]): ConversationState {
  return events.reduce(applyEvent, initConversation(session_id));
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
