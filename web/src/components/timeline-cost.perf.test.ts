/** #1419：TimelineTab 渲染成本探针（手动 perf 车道，不随默认 vitest 跑）。
 *
 *  预算 = 改造前基线 +20%（N=200 / N=500，SSR 单帧，30 次中位数）。基线由
 *  `__timeline_baseline.perf.test.ts` 在同一夹具上实测：9.026ms / 10.385ms。
 *  展开只挂载已渲染卡片内部，窗口行数不变——行为断言见 StepDetail.timeline.test.tsx。
 */
import { beforeAll, describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { EventType } from '../types';
import type { ConversationState } from '../types';
import { initConversation, applyEvent } from '../lib/projection';
import { TimelineTab } from './StepDetail';

const BASELINE_MS: Record<number, number> = { 200: 9.026, 500: 10.385 };

function conv(n: number): ConversationState {
  let s = initConversation('perf');
  for (let i = 0; i < n; i++) {
    s = applyEvent(s, {
      type: i % 10 === 0 ? EventType.TOOL_CALL : EventType.MODEL_DELTA,
      data: i % 10 === 0 ? { tool_call_id: `t${i}`, tool_name: 'bash', args: { command: 'ls' } } : { delta: 'x' },
      seq: i,
      run_id: `r${Math.floor(i / 50)}`,
      step_id: 1,
      session_id: 'perf',
      time: new Date(Date.UTC(2026, 9, 20, 10, 0, i % 60)).toISOString(),
    });
  }
  return s;
}

/** 5 轮 × 30 次，取「最小轮中位数」：单轮中位数会被偶发 GC/机器负载整轮抬高
 *  （实测同一夹具 11ms 与 60ms 交替出现），perf 车道要的是回归信号而不是噪声。 */
function medianMs(fn: () => string): number {
  for (let i = 0; i < 10; i++) fn();
  let best = Number.POSITIVE_INFINITY;
  for (let round = 0; round < 5; round++) {
    const samples: number[] = [];
    for (let i = 0; i < 30; i++) {
      const t0 = performance.now();
      fn();
      samples.push(performance.now() - t0);
    }
    samples.sort((a, b) => a - b);
    best = Math.min(best, samples[15]);
  }
  return best;
}

describe('#1419 TimelineTab render cost', () => {
  beforeAll(() => {
    // 共享预热：首个用例若独自承担 JIT/模块初始化，会把 N=200 的轮中位数整体抬高。
    const warm = conv(200);
    for (let i = 0; i < 20; i++) {
      renderToString(createElement(TimelineTab, { conversation: warm, onFocusEvent: () => {} }));
    }
  });
  for (const n of [200, 500]) {
    it(`N=${n} 中位数 ≤ 基线 +20%`, () => {
      const c = conv(n);
      const render = () => renderToString(createElement(TimelineTab, { conversation: c, onFocusEvent: () => {} }));
      const budget = BASELINE_MS[n] * 1.2;
      // 机器负载噪声：整轮中位数会被一次调度尖峰抬高（实测 9→11ms 且下一轮回落）。
      // 超预算时最多重测 3 次，仍超才失败——不抬高预算，也不静默吞掉真实回归。
      let ms = medianMs(render);
      let attempts = 1;
      while (ms > budget && attempts < 3) {
        ms = medianMs(render);
        attempts += 1;
      }
      const html = render();
      console.log(`N=${n} median=${ms.toFixed(3)}ms budget=${budget.toFixed(3)}ms attempts=${attempts} bytes=${html.length} rows=${(html.match(/timeline-row/g) ?? []).length}`);
      expect(ms).toBeLessThanOrEqual(budget);
    });
  }
});
