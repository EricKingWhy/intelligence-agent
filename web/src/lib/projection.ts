/** Event projection — pure functions turning AgentEvents into ConversationState.
 *
 * This is the ONLY place where events get interpreted into view-models.
 * No component mutates state directly; they all dispatch events here.
 * This satisfies invariant #22 (Web UI maintains no second truth) —
 * the events ARE the truth, this just projects them.
 */

import type { AgentEvent, ConversationState, Turn, ToolCall } from '../types';
import { EventType } from '../types';

export function initConversation(session_id: string): ConversationState {
  return {
    session_id,
    turns: [],
    active_step_id: null,
    run_status: 'idle',
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
  };

  const { type, data, step_id } = event;

  switch (type) {
    case EventType.USER_MESSAGE: {
      const step = (data.step as number) ?? step_id ?? next.turns.length + 1;
      const turn = findOrCreateTurn(next, step);
      turn.user_message = String(data.content ?? '');
      break;
    }

    case EventType.RUN_STARTED: {
      next.run_status = 'running';
      break;
    }

    case EventType.MODEL_STARTED: {
      const step = (data.step as number) ?? step_id ?? 1;
      next.active_step_id = step;
      const turn = findOrCreateTurn(next, step);
      turn.model = { text: '', status: 'streaming' };
      turn.status = 'streaming';
      break;
    }

    case EventType.MODEL_DELTA: {
      const step = step_id ?? next.active_step_id ?? 1;
      const turn = findOrCreateTurn(next, step);
      turn.model.text += String(data.delta ?? '');
      turn.model.status = 'streaming';
      break;
    }

    case EventType.MODEL_COMPLETED: {
      const step = step_id ?? next.active_step_id ?? 1;
      const turn = findOrCreateTurn(next, step);
      // Final content may include consolidated text — prefer it over accumulated delta.
      turn.model.text = String(data.content ?? turn.model.text);
      turn.model.status = 'done';
      // If model emitted tool_calls, attach them as tool entries.
      const tool_calls = data.tool_calls as
        | Array<{ id: string; name: string; args: Record<string, unknown> }>
        | undefined;
      if (Array.isArray(tool_calls)) {
        for (const tc of tool_calls) {
          if (!turn.tools.find((t) => t.tool_call_id === tc.id)) {
            const call: ToolCall = {
              tool_call_id: tc.id,
              name: tc.name,
              args: tc.args ?? {},
              status: 'running',
            };
            turn.tools.push(call);
          }
        }
      }
      break;
    }

    case EventType.TOOL_STARTED: {
      const step = step_id ?? next.active_step_id ?? 1;
      const turn = findOrCreateTurn(next, step);
      const id = String(data.tool_call_id ?? '');
      if (!turn.tools.find((t) => t.tool_call_id === id)) {
        turn.tools.push({
          tool_call_id: id,
          name: String(data.name ?? 'unknown'),
          args: (data.args as Record<string, unknown>) ?? {},
          status: 'running',
          started_at: new Date().toISOString(),
        });
      }
      break;
    }

    case EventType.TOOL_COMPLETED: {
      const step = step_id ?? next.active_step_id ?? 1;
      const turn = findOrCreateTurn(next, step);
      const id = String(data.tool_call_id ?? '');
      const tool = turn.tools.find((t) => t.tool_call_id === id);
      if (tool) {
        const success = data.status !== 'error' && data.error_code == null;
        tool.status = success ? 'success' : 'failed';
        tool.result = data.result ?? data.output;
        if (data.diff && typeof data.diff === 'object') {
          tool.diff = data.diff as ToolCall['diff'];
        }
        tool.completed_at = new Date().toISOString();
      }
      break;
    }

    case EventType.RUN_COMPLETED: {
      next.run_status = 'completed';
      next.active_step_id = null;
      for (const turn of next.turns) {
        if (turn.status === 'streaming') turn.status = 'done';
      }
      break;
    }

    case EventType.RUN_FAILED: {
      next.run_status = 'failed';
      next.active_step_id = null;
      for (const turn of next.turns) {
        if (turn.status === 'streaming') turn.status = 'failed';
      }
      break;
    }

    default:
      // Unknown event types are ignored — forward-compatible.
      break;
  }

  return next;
}

/** Rebuild full conversation from a history of durable events (on page load). */
export function projectHistory(session_id: string, events: AgentEvent[]): ConversationState {
  return events.reduce(applyEvent, initConversation(session_id));
}
