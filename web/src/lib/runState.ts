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
import { EventType, type AgentEvent, type ConversationState } from '../types';

/**
 * run 终态词汇——镜像后端 `session/event.py:RUN_TERMINAL_TYPES`。
 *
 * 「这个 run 收口了吗」是**一个**判断，全前端只有这一个集合：抄第二份就会
 * 漏掉后加的类型。T8（#138）新增 `run/interrupted` 时正好踩了这个坑——
 * `isRecoverableRun` / `StepDetail` 的 run 时长 / `useSession` 的终态检测
 * 三处各自枚举，全都漏了它，于是崩溃会话经后端恢复后「恢复会话」按钮永不
 * 消失、点它又是一次 no-op（用户实测症状：「点了什么反应也没有」）。
 */
export const RUN_TERMINAL_TYPES: ReadonlySet<string> = new Set<string>([
  EventType.RUN_COMPLETED,
  EventType.RUN_FAILED,
  EventType.RUN_INTERRUPTED,
]);

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

/**
 * 未配对的 tool_call id 集合（dangling）——纯事件真值，无状态。
 *
 * `isRecoverableRun` 与 recover 成功后的「修了几条」统计共用这一份实现：
 * 两处各写一遍配对逻辑就会漂移（同一个 tool_call 在一处算 dangling、
 * 另一处不算）。append-only 日志下**只增不改**，所以调用方可以安全地
 * 用 before/after 两个集合做差得到「本次恢复修好的条数」。
 */
export function unpairedToolCallIds(events: AgentEvent[]): Set<string> {
  const pending = new Set<string>();
  for (const e of events) {
    if (e.type === EventType.TOOL_CALL) {
      const id = String((e.data as Record<string, unknown> | undefined)?.tool_call_id ?? '');
      if (id) pending.add(id);
    } else if (e.type === EventType.TOOL_RESULT) {
      const id = String((e.data as Record<string, unknown> | undefined)?.tool_call_id ?? '');
      pending.delete(id);
    }
  }
  return pending;
}

/** 最后一个 run 是否有头无尾（`run/started` 之后一个终态都没出现）。
 *
 *  与 `isRecoverableRun` 共用同一趟扫描：恢复反馈要区分「回填了工具结果」与
 *  「补齐了 run 终态」两种修复，而 `repaired` 只数前者——只报 repaired 会把
 *  一次真实的终态修复说成「无可修复项」。
 *  无 run 的裸会话（仅生命周期事件）返回 false（无东西可恢复）。 */
export function hasUnterminatedRun(events: AgentEvent[]): boolean {
  return scanRuns(events).unterminated;
}

/** 一并取出「有没有 run」与「最后一个 run 是否收口」——两个消费者的共同扫描。 */
function scanRuns(events: AgentEvent[]): { hasRun: boolean; unterminated: boolean } {
  let hasRun = false;
  let lastRunTerminated = false;
  for (const e of events) {
    if (e.type === EventType.RUN_STARTED) {
      hasRun = true;
      lastRunTerminated = false;
    } else if (RUN_TERMINAL_TYPES.has(e.type)) {
      lastRunTerminated = true;
    }
  }
  return { hasRun, unterminated: hasRun && !lastRunTerminated };
}

/** 判断会话是否值得展示「恢复」入口。后端建议语义（OR）：
 *  ①最后一个 run 缺少终态（`RUN_TERMINAL_TYPES` 里任一都没出现）；
 *  ②存在无配对 tool/result 的 tool_call（dangling）。
 *  以事件真值判定（不变量 #22），可从 PROJECT 外重复调用（纯函数）。
 *  注意：干净结束的 run（有终态且无 dangling）是终态——不可恢复，
 *  与旧实现「最后事件非 run/completed 即可见」不同（会把干净失败也标成可恢复）。
 *  `run/interrupted` 同样是终态：后端启动扫描已按 Operation Ledger 回填过，
 *  此时仍显示入口等于给用户一个必然 no-op 的按钮。
 *  裸会话（有 dangling 但一个 run/started 都没有）仍然返回 false——比后端字面
 *  语义保守：这种日志本身畸形，暴露「恢复」入口既不解决问题又给出错误承诺。 */
export function isRecoverableRun(events: AgentEvent[]): boolean {
  // 复用同一次扫描：原先 hasRun / hasUnterminatedRun 各扫一遍，同一份事件走两趟。
  const { hasRun, unterminated } = scanRuns(events);
  if (!hasRun) return false;
  return unterminated || unpairedToolCallIds(events).size > 0;
}

/** recover 成功后的反馈文案——结局不能合并成一句「已恢复」：用户看不出后端
 *  到底修了什么，就会把「修好了」与「按钮坏了」看成同一个画面（这正是本缺陷的
 *  原始症状）。纯函数，便于把「不夸大、不误报修复量」锁进单测。
 *
 *  两个「仍未修完」的原因是**分开**传的，不能压成一个布尔：`isRecoverableRun`
 *  是「缺终态 OR 有 dangling」，把布尔当原因就会在「终态已补、只是还有个悬空
 *  tool_call」时误报成「仍缺 run 终态」——和原来那条谎报同一类错。 */
export function recoverDoneMessage(r: {
  repaired: number;
  terminalRepaired: boolean;
  stillUnterminated: boolean;
  stillDangling: boolean;
}): string {
  const remaining =
    r.stillUnterminated && r.stillDangling
      ? '仍缺 run 终态且有未配对工具调用'
      : r.stillUnterminated
        ? '仍缺 run 终态'
        : r.stillDangling
          ? '仍有未配对工具调用'
          : null;
  const fixed =
    r.repaired > 0
      ? r.terminalRepaired
        ? `已恢复：回填 ${r.repaired} 条工具结果，并补齐 run 终态`
        : `已恢复：回填 ${r.repaired} 条工具结果`
      : r.terminalRepaired
        ? '已恢复：补齐 run 终态'
        : null;
  if (fixed) return remaining ? `${fixed}（${remaining}，可重试）` : fixed;
  if (remaining) return `已恢复：本次无回填项（${remaining}，可重试）`;
  return '已恢复：无可修复项（会话事件已完整）';
}
