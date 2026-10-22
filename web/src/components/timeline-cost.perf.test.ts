/** #1419：TimelineTab 渲染成本探针（手动 perf 车道，不随默认 vitest 跑）。
 *
 *  预算口径（票面 AC，不再重定）：**改造前基线 × 1.2**。
 *  基线 provenance：`9.026ms @N=200 / 10.385ms @N=500` 由同一夹具、同一
 *  `medianMs`（5 轮 × 30 次的最小轮中位数）在融合改造前实测（#1419 首轮，
 *  见 issue 与 worklog-1419-perf.md）；预算 = 10.831ms / 12.462ms。
 *
 *  修复轮曾把基线改成 18.7ms（每行 lucide 图标把 SSR 成本抬到 ~22ms），
 *  那是重定 AC，已回退。优化轮把图标做成**零 DOM 节点**（CSS mask + 伪元素），
 *  相对改造前实现只剩 meta 文本包装层一个元素（+~0.55ms/200 行），
 *  本文件基线仍保持 9.026/10.385。
 *
 *  复跑：
 *    `cd web && npx vitest run -c vitest.perf.config.ts src/components/timeline-cost.perf.test.ts --reporter=verbose`
 *  每个用例都会打印 `N=<n> median=<ms>ms budget=<ms>ms`。超预算时重测最多
 *  3 次并取**最小**读数——机器负载只会把单次读数抬高，回归信号应当是
 *  "最好的一次也超预算"。
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
      type: i % 10 === 0 ? EventType.TOOL_CALL : EventType.TEXT_DELTA,
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
      // 超预算时最多重测 3 次并取**最小**读数，仍超才失败——不抬高预算，也不静默
      // 吞掉真实回归（负载只会抬高读数，最好的一次仍超预算才算回归）。
      let ms = medianMs(render);
      let attempts = 1;
      while (ms > budget && attempts < 3) {
        ms = Math.min(ms, medianMs(render));
        attempts += 1;
      }
      const html = render();
      console.log(`N=${n} median=${ms.toFixed(3)}ms budget=${budget.toFixed(3)}ms attempts=${attempts} bytes=${html.length} rows=${(html.match(/timeline-row/g) ?? []).length}`);
      expect(ms).toBeLessThanOrEqual(budget);
      /* 同步采样循环不可抢占：超时只能事后判。慢机器上整文件实测 ~92s（5×30×2 轮），
       * 留 120s 让断言本身（预算）决定成败，而不是 vitest 的 5s 默认值。 */
    }, 120_000);
  }
});
