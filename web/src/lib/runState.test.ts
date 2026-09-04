import { describe, expect, it } from 'vitest';
import { deriveRunPulse } from './runState';
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
