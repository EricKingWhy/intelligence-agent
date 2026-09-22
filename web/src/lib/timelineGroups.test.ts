/** UI-03：run 分组纯函数契约（分组/序数/状态推导/计数）。
 *  时间线尾窗裁剪下组号不得重排——ordinal 必须来自全会话遍历。 */
import { describe, expect, it } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent } from '../types';
import { countRuns, groupEventsByRun } from './timelineGroups';

function e(type: AgentEvent['type'], seq: number, runId?: string): AgentEvent {
  return { type, data: {}, seq, ...(runId !== undefined ? { run_id: runId } : {}), session_id: 's' } as AgentEvent;
}

describe('groupEventsByRun — 按 run_id 首现顺序分组', () => {
  it('双 run：两组、序数 1/2、各自计数', () => {
    const groups = groupEventsByRun([
      e(EventType.SESSION_STARTED, 1, 'r1'),
      e(EventType.RUN_STARTED, 2, 'r1'),
      e(EventType.RUN_COMPLETED, 3, 'r1'),
      e(EventType.RUN_STARTED, 4, 'r2'),
      e(EventType.USER_MESSAGE, 5, 'r2'),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ runId: 'r1', ordinal: 1, status: 'completed', count: 3, start: 0 });
    expect(groups[1]).toMatchObject({ runId: 'r2', ordinal: 2, status: 'running', count: 2, start: 3 });
  });

  it('交错 run_id 不合并：r1 出现后再回 r2 再回 r1 → 各成新组；序数按首次出现（回段仍叫 Run 1）', () => {
    const groups = groupEventsByRun([
      e(EventType.RUN_STARTED, 1, 'r1'),
      e(EventType.RUN_STARTED, 2, 'r2'),
      e(EventType.RUN_STARTED, 3, 'r1'),
    ]);
    expect(groups.map((g) => g.runId)).toEqual(['r1', 'r2', 'r1']);
    expect(groups.map((g) => g.ordinal)).toEqual([1, 2, 1]);
  });

  it('run_id 缺失事件：有打开组时归属当前组；无组时进领头 null 组（ordinal 0）', () => {
    const leading = groupEventsByRun([e(EventType.SESSION_STARTED, 1), e(EventType.RUN_STARTED, 2, 'r1')]);
    expect(leading).toHaveLength(2);
    expect(leading[0]).toMatchObject({ runId: null, ordinal: 0, count: 1, start: 0 });
    expect(leading[1]).toMatchObject({ runId: 'r1', ordinal: 1, count: 1, start: 1 });

    const attached = groupEventsByRun([e(EventType.RUN_STARTED, 1, 'r1'), e(EventType.MODEL_DELTA, 2)]);
    expect(attached).toHaveLength(1);
    expect(attached[0]).toMatchObject({ runId: 'r1', count: 2 });
  });

  it('状态推导：completed / failed / cancelled / interrupted；无终态 → running', () => {
    const done = groupEventsByRun([e(EventType.RUN_STARTED, 1, 'a'), e(EventType.RUN_COMPLETED, 2, 'a')]);
    expect(done[0].status).toBe('completed');
    const failed = groupEventsByRun([e(EventType.RUN_STARTED, 1, 'b'), e(EventType.RUN_FAILED, 2, 'b')]);
    expect(failed[0].status).toBe('failed');
    const stopped = groupEventsByRun([e(EventType.RUN_STARTED, 1, 'c'), e(EventType.RUN_INTERRUPTED, 2, 'c')]);
    expect(stopped[0].status).toBe('interrupted');
    const live = groupEventsByRun([e(EventType.RUN_STARTED, 1, 'd')]);
    expect(live[0].status).toBe('running');
  });

  it('run/failed.reason=cancelled → cancelled（用户取消 ≠ 失败，A-02）', () => {
    const cancelled: AgentEvent = {
      type: EventType.RUN_FAILED, data: { reason: 'cancelled' }, seq: 2, run_id: 'k', session_id: 's',
    } as AgentEvent;
    const groups = groupEventsByRun([e(EventType.RUN_STARTED, 1, 'k'), cancelled]);
    expect(groups[0].status).toBe('cancelled');

    // 无 reason / 其它 reason（真实失败）仍是 failed——不把失败洗成取消。
    const other: AgentEvent = {
      type: EventType.RUN_FAILED, data: { reason: 'model_error' }, seq: 2, run_id: 'm', session_id: 's',
    } as AgentEvent;
    const plain: AgentEvent = {
      type: EventType.RUN_FAILED, data: {}, seq: 2, run_id: 'n', session_id: 's',
    } as AgentEvent;
    expect(groupEventsByRun([e(EventType.RUN_STARTED, 1, 'm'), other])[0].status).toBe('failed');
    expect(groupEventsByRun([e(EventType.RUN_STARTED, 1, 'n'), plain])[0].status).toBe('failed');
  });

  it('脏数据多终态并存：interrupted > failed（含 cancelled）> completed 取更醒目者', () => {
    const dirty = groupEventsByRun([
      e(EventType.RUN_STARTED, 1, 'x'),
      e(EventType.RUN_COMPLETED, 2, 'x'),
      e(EventType.RUN_FAILED, 3, 'x'),
      e(EventType.RUN_INTERRUPTED, 4, 'x'),
    ]);
    expect(dirty[0].status).toBe('interrupted');

    // completed → cancelled：取消比完成醒目（与 completed → failed 同序）。
    const cancelledEvent = (runId: string, seq: number): AgentEvent => ({
      type: EventType.RUN_FAILED, data: { reason: 'cancelled' }, seq, run_id: runId, session_id: 's',
    } as AgentEvent);
    expect(groupEventsByRun([e(EventType.RUN_STARTED, 1, 'y'), e(EventType.RUN_COMPLETED, 2, 'y'), cancelledEvent('y', 3)])[0].status)
      .toBe('cancelled');
    // cancelled → completed：已取消是终态，后续 completed 不回退（与 failed → completed 同序）。
    expect(groupEventsByRun([e(EventType.RUN_STARTED, 1, 'z'), cancelledEvent('z', 2), e(EventType.RUN_COMPLETED, 3, 'z')])[0].status)
      .toBe('cancelled');
  });

  it('空数组 → 空分组', () => {
    expect(groupEventsByRun([])).toEqual([]);
  });
});

describe('countRuns — 头标「N runs」计数', () => {
  it('distinct run_id 数；null 不计', () => {
    expect(countRuns([e(EventType.RUN_STARTED, 1, 'a'), e(EventType.RUN_STARTED, 2, 'b'), e(EventType.MODEL_DELTA, 3)])).toBe(2);
    expect(countRuns([e(EventType.MODEL_DELTA, 1)])).toBe(0);
  });
});
