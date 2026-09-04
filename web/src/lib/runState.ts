/** Run Pulse state derivation — signature #1 (docs/UI_DESIGN_DECISIONS.md).
 *
 * Pure functions: ConversationState (+ optional streaming flag) → a single
 * run-state descriptor (state + label + class + icon). Three channels by
 * contract (icon + color + text, never color-only). Color/animation live in
 * CSS keyed by `className`; the icon is a lucide component referenced by type
 * so consumers stay free of parallel lookup tables.
 *
 * This is projection-adjacent truth: derived ONLY from ConversationState fields
 * that themselves come from events — no fabrication (zero-fake-metrics rule).
 */

import { Activity, CircleDashed, Loader2, SquareCheckBig, SquareX } from 'lucide-react';
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
  /** CSS class for the run-pulse element (color + animation channels). */
  className: string;
  /** Icon channel — lucide component type for the consumer to instantiate. */
  Icon: typeof Activity;
}

interface RunPulseRow {
  label: string;
  className: string;
  Icon: typeof Activity;
}

const PULSE_TABLE: Record<RunPulseState, RunPulseRow> = {
  idle: { label: '空闲', className: 'pulse-idle', Icon: CircleDashed },
  thinking: { label: '思考中', className: 'pulse-thinking', Icon: Loader2 },
  tool: { label: '执行工具', className: 'pulse-tool', Icon: Loader2 },
  completed: { label: '已完成', className: 'pulse-completed', Icon: SquareCheckBig },
  failed: { label: '失败', className: 'pulse-failed', Icon: SquareX },
};

/** Derive the run pulse for a conversation.
 *  `streaming` = useSession live mode (SSE attached). A conversation whose run
 *  hasn't emitted run/completed|failed but is no longer receiving frames is
 *  still 'running-family' — decided by run_status, not by client timers. */
export function deriveRunPulse(
  conversation: ConversationState | null,
  streaming: boolean,
): RunPulseDescriptor {
  if (!conversation) {
    const r = PULSE_TABLE.idle;
    return { state: 'idle', label: r.label, className: r.className, Icon: r.Icon };
  }

  switch (conversation.run_status) {
    case 'completed': {
      const r = PULSE_TABLE.completed;
      return { state: 'completed', label: r.label, className: r.className, Icon: r.Icon };
    }
    case 'failed': {
      const r = PULSE_TABLE.failed;
      return { state: 'failed', label: r.label, className: r.className, Icon: r.Icon };
    }
    case 'running': {
      // Sub-state: latest event is an unfinished tool call → 'tool',
      // otherwise the model segment is being streamed → 'thinking'.
      const activeTurn =
        conversation.active_step_id !== null
          ? conversation.turns.find((t) => t.step_id === conversation.active_step_id)
          : undefined;
      const hasRunningTool = activeTurn?.tools.some((t) => t.status === 'running') ?? false;
      const r = hasRunningTool ? PULSE_TABLE.tool : PULSE_TABLE.thinking;
      return {
        state: hasRunningTool ? 'tool' : 'thinking',
        label: r.label,
        className: r.className,
        Icon: r.Icon,
      };
    }
    case 'idle': {
      // run_status never left idle but frames may still be arriving (pre-
      // run/started). While live-streaming show thinking, else idle.
      const r = streaming ? PULSE_TABLE.thinking : PULSE_TABLE.idle;
      return {
        state: streaming ? 'thinking' : 'idle',
        label: r.label,
        className: r.className,
        Icon: r.Icon,
      };
    }
  }
}
