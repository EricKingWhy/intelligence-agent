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
import type { AgentEvent, ConversationState } from '../types';

export type RunPulseState =
  | 'idle' // no conversation or nothing has happened
  | 'thinking' // run active, model segment streaming, no tools yet
  | 'tool' // run active, latest tool call still running
  | 'completed'
  | 'cancelled' // run/failed.data.reason === 'cancelled'（客户端断连，中断 ≠ 错误）
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
  // 已取消：客户端断连（run/failed.reason=cancelled，da394a9）——中性色，非红色报错
  cancelled: { label: '已取消', className: 'pulse-cancelled', Icon: SquareX },
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
      // 取消 ≠ 失败：断连导致的 run/failed 走中性「已取消」通道（da394a9 语义）
      const r = conversation.run_cancelled ? PULSE_TABLE.cancelled : PULSE_TABLE.failed;
      return {
        state: conversation.run_cancelled ? 'cancelled' : 'failed',
        label: r.label,
        className: r.className,
        Icon: r.Icon,
      };
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

// ── 会话健康度：恢复入口可见性（da394a9 §二.2 建议语义） ──

/** 判断会话是否值得展示「恢复」入口。后端建议语义（OR）：
 *  ①最后一个 run 缺少终态（run/completed 或 run/failed 都没有）；
 *  ②存在无配对 tool/result 的 tool_call（dangling）。
 *  以事件真值判定（不变量 #22），可从 PROJECT 外重复调用（纯函数）。
 *  注意：干净失败的 run（run/failed 且无 dangling）是终态——不可恢复，
 *  与旧实现「最后事件非 run/completed 即可见」不同（会把干净失败也标成可恢复）。 */
export function isRecoverableRun(events: AgentEvent[]): boolean {
  let hasRun = false;
  let lastRunTerminated = false;
  const pendingCalls = new Set<string>();
  let dangling = false;
  for (const e of events) {
    switch (e.type) {
      case 'run/started':
        hasRun = true;
        lastRunTerminated = false;
        break;
      case 'run/completed':
      case 'run/failed':
        lastRunTerminated = true;
        break;
      case 'tool/call': {
        const id = String((e.data as Record<string, unknown> | undefined)?.tool_call_id ?? '');
        if (id) pendingCalls.add(id);
        break;
      }
      case 'tool/result': {
        const id = String((e.data as Record<string, unknown> | undefined)?.tool_call_id ?? '');
        pendingCalls.delete(id);
        break;
      }
      default:
        break;
    }
  }
  if (pendingCalls.size > 0) dangling = true;
  // 无 run 的裸会话（仅生命周期事件）：无东西可恢复 → false（比后端字面语义更保守）
  if (!hasRun) return false;
  return !lastRunTerminated || dangling;
}
