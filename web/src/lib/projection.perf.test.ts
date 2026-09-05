/** 投影层基准（HANDOFF_PERF_FRONTEND §4.3 复现脚本的测试化）。
 *
 * 手动车道——不进默认 CI（数字随机器漂移，预算断言由 P2-6 perf 车道承担）。
 * 运行：npx vitest run src/lib/projection.perf.test.ts
 */
import { describe, it } from 'vitest';
import type { AgentEvent } from '../types';
import { EventType } from '../types';
import { applyEvent, initConversation, projectHistory } from './projection';

function mkDelta(i: number): AgentEvent {
  return {
    type: EventType.MODEL_DELTA,
    data: { delta: 'x' },
    seq: i,
    run_id: 'r',
    step_id: 1,
    session_id: 'b',
  };
}

function mk(n: number) {
  let s = initConversation('b');
  for (let i = 0; i < n; i++) s = applyEvent(s, mkDelta(i));
  return s;
}

describe('bench: applyEvent 单事件成本随事件总数（§4.3 复现）', () => {
  it('100 / 1k / 5k / 20k', () => {
    for (const n of [100, 1000, 5000, 20000]) {
      const s = mk(n);
      const t0 = performance.now();
      for (let i = 0; i < 200; i++) applyEvent(s, mkDelta(999999));
      const us = ((performance.now() - t0) / 200) * 1000;
      console.log(`applyEvent @${n}: ${us.toFixed(1)}µs/事件`);
    }
  });
});

describe('bench: projectHistory 历史重建', () => {
  it('4650 事件', () => {
    const events = Array.from({ length: 4650 }, (_, i) => mkDelta(i));
    const t0 = performance.now();
    projectHistory('b', events);
    console.log(`projectHistory 4650: ${(performance.now() - t0).toFixed(1)}ms`);
  });

  it('20000 事件', () => {
    const events = Array.from({ length: 20000 }, (_, i) => mkDelta(i));
    const t0 = performance.now();
    projectHistory('b', events);
    console.log(`projectHistory 20000: ${(performance.now() - t0).toFixed(1)}ms`);
  });
});
