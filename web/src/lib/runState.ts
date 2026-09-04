/** Run Pulse state derivation — signature #1 (docs/UI_DESIGN_DECISIONS.md).
 *
 * Pure functions: ConversationState (+ optional streaming flag) → a single
 * run-state descriptor (state + label). Color + animation channels live in
 * CSS (`.run-pulse-*` / `.run-badge-*`), the icon channel in TopBar's
 * PULSE_ICON. Three-channel by contract (icon + color + text), never
 * color-only.
 *
 * This is projection-adjacent truth: derived ONLY from ConversationState fields
 * that themselves come from events — no fabrication (zero-fake-metrics rule).
 */

import type { ConversationState } from '../types';

export type RunPulseState =
  | 'idle' // no conversation or nothing has happened
  | 'thinking' // run active, model segment streaming, no tools yet
  | 'tool' // run active, latest tool call still running
  | 'completed'
  | 'failed';

export interface RunPulseDescriptor {
  state: RunPulseState;
  /** Short Chinese label — text channel (never color-only). */
  label: string;
}

const LABELS: Record<RunPulseState, string> = {
  idle: '空闲',
  thinking: '思考中',
  tool: '执行工具',
  completed: '已完成',
  failed: '失败',
};

/** Derive the run pulse for a conversation.
 *  `streaming` = useSession live mode (SSE attached). A conversation whose run
 *  hasn't emitted run/completed|failed but is no longer receiving frames is
 *  still 'running-family' — decided by run_status, not by client timers. */
export function deriveRunPulse(
  conversation: ConversationState | null,
  streaming: boolean,
): RunPulseDescriptor {
  if (!conversation) return { state: 'idle', label: LABELS.idle };

  switch (conversation.run_status) {
    case 'completed':
      return { state: 'completed', label: LABELS.completed };
    case 'failed':
      return { state: 'failed', label: LABELS.failed };
    case 'running': {
      // Sub-state: latest event is an unfinished tool call → 'tool',
      // otherwise the model segment is being streamed → 'thinking'.
      const activeTurn =
        conversation.active_step_id !== null
          ? conversation.turns.find((t) => t.step_id === conversation.active_step_id)
          : undefined;
      const hasRunningTool = activeTurn?.tools.some((t) => t.status === 'running') ?? false;
      return hasRunningTool
        ? { state: 'tool', label: LABELS.tool }
        : { state: 'thinking', label: LABELS.thinking };
    }
    case 'idle':
      // run_status never left idle but frames may still be arriving (pre-
      // run/started). While live-streaming show thinking, else idle.
      return streaming
        ? { state: 'thinking', label: LABELS.thinking }
        : { state: 'idle', label: LABELS.idle };
  }
}
