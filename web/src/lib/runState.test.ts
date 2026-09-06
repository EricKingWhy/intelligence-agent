import { describe, expect, it } from 'vitest';
import { deriveRunPulse, isRecoverableRun } from './runState';
import { initConversation, applyEvent } from './projection';
import { EventType } from '../types';
import type { AgentEvent } from '../types';

function ev(type: string, data: Record<string, unknown>, step_id: number | null = 1): AgentEvent {
  return { type, data, seq: null, run_id: null, step_id };
}

describe('deriveRunPulse', () => {
  it('idle when no conversation', () => {
    expect(deriveRunPulse(null, false).state).toBe('idle');
  });

  it('thinking while model streams, tool while tool running', () => {
    let s = initConversation('s1');
    s = applyEvent(s, ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.MODEL_STARTED, {}, 1));
    s = applyEvent(s, ev(EventType.MODEL_DELTA, { delta: 'hi' }, 1));
    expect(deriveRunPulse(s, true).state).toBe('thinking');

    s = applyEvent(
      s,
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }, 1),
    );
    expect(deriveRunPulse(s, true).state).toBe('tool');
    expect(deriveRunPulse(s, true).label).toBe('执行工具');

    s = applyEvent(
      s,
      ev(
        EventType.TOOL_RESULT,
        {
          tool_call_id: 't1',
          content: JSON.stringify({ ok: true, message: 'done', data: null }),
        },
        1,
      ),
    );
    expect(deriveRunPulse(s, true).state).toBe('thinking');
  });

  it('completed / failed terminal states', () => {
    let s = initConversation('s1');
    s = applyEvent(s, ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.RUN_COMPLETED, {}));
    expect(deriveRunPulse(s, false).state).toBe('completed');

    let f = initConversation('s1');
    f = applyEvent(f, ev(EventType.RUN_STARTED, {}));
    f = applyEvent(f, ev(EventType.RUN_FAILED, {}));
    expect(deriveRunPulse(f, false).state).toBe('failed');
    expect(deriveRunPulse(f, false).label).toBe('失败');
  });

  it('idle run_status + not streaming stays idle', () => {
    expect(deriveRunPulse(initConversation('s1'), false).state).toBe('idle');
  });

  it('idle run_status + streaming shows thinking (pre-run/started frames)', () => {
    expect(deriveRunPulse(initConversation('s1'), true).state).toBe('thinking');
    expect(deriveRunPulse(initConversation('s1'), true).label).toBe('思考中');
  });
});

// ── da394a9 批：取消态脉冲 + 恢复可见性 ──

describe('deriveRunPulse — cancelled 通道', () => {
  it('run/failed + run_cancelled → 已取消（中性）而非失败（红）', () => {
    let s = applyEvent(initConversation('s'), ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.RUN_FAILED, { reason: 'cancelled' }));
    const pulse = deriveRunPulse(s, false);
    expect(pulse.state).toBe('cancelled');
    expect(pulse.label).toBe('已取消');
    expect(pulse.className).toBe('pulse-cancelled');
  });

  it('run/failed 无取消标记 → 失败（红）', () => {
    let s = applyEvent(initConversation('s'), ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.RUN_FAILED, {}));
    expect(deriveRunPulse(s, false).state).toBe('failed');
  });
});

describe('isRecoverableRun — 恢复入口可见性（da394a9 §二.2）', () => {
  it('run/started 无终态 → true（中断会话可恢复）', () => {
    const events = [ev(EventType.RUN_STARTED, {})];
    expect(isRecoverableRun(events)).toBe(true);
  });

  it('干净完成 → false；干净失败 → false（终态，不可恢复）', () => {
    const done = [ev(EventType.RUN_STARTED, {}), ev(EventType.RUN_COMPLETED, {})];
    const failed = [ev(EventType.RUN_STARTED, {}), ev(EventType.RUN_FAILED, {})];
    expect(isRecoverableRun(done)).toBe(false);
    expect(isRecoverableRun(failed)).toBe(false);
  });

  it('干净失败 + 未配对 tool_call → true（dangling 可修）', () => {
    const events = [
      ev(EventType.RUN_STARTED, {}),
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }),
      // t1 的 tool/result 丢了（dangling）
      ev(EventType.TOOL_CALL, { tool_call_id: 't2', tool_name: 'bash' }),
      ev(EventType.TOOL_RESULT, { tool_call_id: 't2', content: 'ok' }),
      ev(EventType.RUN_FAILED, {}),
    ];
    expect(isRecoverableRun(events)).toBe(true);
  });

  it('无 run 的裸会话 → false（无可恢复物）', () => {
    expect(isRecoverableRun([ev(EventType.SESSION_STARTED, {})])).toBe(false);
  });
});
