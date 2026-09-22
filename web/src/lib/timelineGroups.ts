/** UI-03：Inspector 时间线的 run 分组——纯函数，SSR/组件层共用。
 *
 *  分组规则（docs/UI_POLISH_TICKETS.md UI-03 规格 1）：
 *  - 按 run_id 的**首次出现顺序**分组；序数（ordinal）在全会话范围编号——
 *    时间线尾窗裁剪后组号不得重排（窗口内看到 Run 2 必须仍叫 Run 2）。
 *  - run_id 缺失的事件（如会话开场帧）归属当前已打开的组；尚无组时归入
 *    领头的 runId=null 组（渲染层标「会话」）。
 *  - 组状态由组内终态事件推导；多种终态并存时按严重度取，不静默丢。
 *  - `run/failed` 带 `reason === 'cancelled'` 是**用户取消**（≠ 错误，da394a9）：
 *    与顶栏脉冲同一口径，否则同一次取消会出现「已取消 / 失败」两种说法
 *    （真机审计 A-02，`docs/FRONTEND_ISSUES_LOG.md` 第十五轮）。 */
import { EventType } from '../types';
import type { AgentEvent } from '../types';

export type RunGroupStatus = 'completed' | 'failed' | 'cancelled' | 'interrupted' | 'running';

export interface RunGroup {
  /** run_id（缺失 = 领头的「会话」组） */
  runId: string | null;
  /** 1-based 序数（仅 runId 组；null 组恒 0） */
  ordinal: number;
  status: RunGroupStatus;
  /** 组内事件数 */
  count: number;
  /** 组在原数组中的起始下标（渲染层用区间求与尾窗的交集） */
  start: number;
}

/** 组状态严重度：并存的脏数据取更醒目者。取消与失败同级（同一终态的两种归因），
 *  interrupted 更醒目、completed 最弱——与原「interrupted > failed > completed」
 *  逐例等价（见 timelineGroups.test.ts 的并存用例）。 */
const STATUS_RANK: Record<RunGroupStatus, number> = {
  running: 0,
  completed: 1,
  cancelled: 2,
  failed: 2,
  interrupted: 3,
};

/** 单条事件的终态归因；非终态 → null。 */
function terminalStatusOf(e: AgentEvent): RunGroupStatus | null {
  if (e.type === EventType.RUN_COMPLETED) return 'completed';
  if (e.type === EventType.RUN_INTERRUPTED) return 'interrupted';
  if (e.type === EventType.RUN_FAILED) {
    const reason = (e.data as Record<string, unknown> | undefined)?.reason;
    return reason === 'cancelled' ? 'cancelled' : 'failed';
  }
  return null;
}

export function groupEventsByRun(events: AgentEvent[]): RunGroup[] {
  const groups: RunGroup[] = [];
  // 序数 = run 在会话中**首次出现**的次序（跨交错段稳定：r1,r2,r1 都叫 Run 1）。
  const ordinalOfRun = new Map<string, number>();
  let current: RunGroup | null = null;

  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    const runId = e.run_id ?? null;
    if (!current || (runId !== null && current.runId !== runId)) {
      let ordinal = 0;
      if (runId !== null) {
        let next = ordinalOfRun.get(runId);
        if (next === undefined) {
          next = ordinalOfRun.size + 1;
          ordinalOfRun.set(runId, next);
        }
        ordinal = next;
      }
      current = { runId, ordinal, status: 'running', count: 0, start: i };
      groups.push(current);
    }
    current.count += 1;
    const terminal = terminalStatusOf(e);
    if (terminal && STATUS_RANK[terminal] > STATUS_RANK[current.status]) {
      current.status = terminal;
    }
  }
  return groups;
}

/** 会话内出现过的 distinct run 数（头标「N runs · M 事件」用；null 不计）。 */
export function countRuns(events: AgentEvent[]): number {
  const seen = new Set<string>();
  for (const e of events) {
    if (e.run_id) seen.add(e.run_id);
  }
  return seen.size;
}
