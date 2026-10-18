import { EventType } from '../types';
import type { AgentEvent } from '../types';
import { groupEventsByRun, type RunGroup } from './timelineGroups';
import { RUN_TERMINAL_TYPES } from './runState';

const LONG_RUN_MS = 10 * 60 * 1000;

export interface TaskReviewSummary {
  runCount: number;
  incompleteCount: number;
  hasTerminalRun: boolean;
  hasRunningRun: boolean;
  emphasizeDetails: boolean;
  /** #1325 S2：最近一个已终态 run 的 run_id（「标记已读」的作用域）。无终态 run
   *  时为 null——横条本来就不渲染，调用方不需要再猜。 */
  latestTerminalRunId: string | null;
}

function durationMs(events: readonly AgentEvent[]): number | null {
  const startedAt = events.find(
    (event) => event.type === EventType.RUN_STARTED && event.time,
  )?.time;
  const terminalAt = [...events]
    .reverse()
    .find((event) =>
      event.time &&
      (event.type === EventType.RUN_COMPLETED ||
        event.type === EventType.RUN_FAILED ||
        event.type === EventType.RUN_INTERRUPTED),
    )?.time;
  if (!startedAt || !terminalAt) return null;

  const start = Date.parse(startedAt);
  const end = Date.parse(terminalAt);
  return Number.isFinite(start) && Number.isFinite(end) && end >= start ? end - start : null;
}

function runGroup(events: AgentEvent[]): RunGroup | undefined {
  return groupEventsByRun(events)[0];
}

export function summarizeTaskRuns(events: readonly AgentEvent[]): TaskReviewSummary {
  const byRun = new Map<string, AgentEvent[]>();
  for (const event of events) {
    if (!event.run_id) continue;
    const runEvents = byRun.get(event.run_id) ?? [];
    runEvents.push(event);
    byRun.set(event.run_id, runEvents);
  }

  const runs = [...byRun.values()].flatMap((runEvents) => {
    const group = runGroup(runEvents);
    return group ? [{ group, duration: durationMs(runEvents) }] : [];
  });
  // 「最近」= **终态事件最晚**的那个 run，按事件序取（`Map` 迭代序是首次出现序：
  // 两个 run 交错时 r1 先起后完、r2 后起先完，按首次出现序会把 r2 当成最近的）。
  let latestTerminalRunId: string | null = null;
  for (const event of events) {
    if (event.run_id && RUN_TERMINAL_TYPES.has(event.type)) {
      latestTerminalRunId = event.run_id;
    }
  }
  const incompleteCount = runs.filter(
    ({ group }) => group.status === 'failed' || group.status === 'interrupted',
  ).length;
  const hasTerminalRun = runs.some(({ group }) => group.status !== 'running');
  const hasRunningRun = runs.some(({ group }) => group.status === 'running');
  const hasLongRun = runs.some(({ duration }) => duration !== null && duration > LONG_RUN_MS);

  return {
    runCount: runs.length,
    incompleteCount,
    hasTerminalRun,
    hasRunningRun,
    emphasizeDetails: incompleteCount > 0 || runs.length >= 3 || hasLongRun,
    latestTerminalRunId,
  };
}
