/** N2（#271）成本探针——**基线复现脚本**，与 `f1-cost-probe.perf.test.ts` 同属 perf 车道
 *  （`vitest.config.ts` 已排除 `*.perf.test.ts`，不进默认 `npm test`）。
 *
 * 为什么需要它：本票把 `TimelineTab` 的 run 分组从「依赖 `events` 引用（= 永不重算）」
 * 改成「依赖 `eventsVersion`（= 每次 `events.push` 都重算）」。正确性由此修好，
 * 但**重算频率变了**：从 0 次/会话 变成 1 次/提交。所以「单次重算多少钱」必须有数字，
 * 否则「修好正确性」容易被误读成「顺手引入每帧线性扫描」。
 *
 * 取数口径（PERF_BASELINE §2.2）：绝对值只用于说明量级，判据是**单次成本 × 40 次/秒**
 * 的占比。运行：
 *   node node_modules/vitest/vitest.mjs run -c vitest.perf.config.ts src/lib/n2-cost-probe.perf.test.ts
 */
import { describe, expect, it } from 'vitest';
import type { AgentEvent } from '../types';
import { EventType } from '../types';
import { applyEvent, initConversation } from './projection';
import { groupEventsByRun } from './timelineGroups';

/** n 个事件、每 100 个换一个 run_id——贴近真实会话（run 串行、每个 run 内多事件）。 */
function mkEvents(n: number): AgentEvent[] {
  return Array.from({ length: n }, (_, i) => ({
    type: EventType.MODEL_DELTA,
    data: { delta: 'x' },
    seq: i,
    run_id: `r${Math.floor(i / 100)}`,
    step_id: Math.floor(i / 100) + 1,
    session_id: 'b',
  }));
}

function median(values: number[]): number {
  const s = [...values].sort((a, b) => a - b);
  return s[Math.floor(s.length / 2)];
}

describe('N2 探针：groupEventsByRun 单次成本（本票唯一被改变的运行时行为）', () => {
  it('200 / 2000 / 20000 事件（n=50 取中位数）', () => {
    for (const n of [200, 2000, 20000]) {
      const events = mkEvents(n);
      const samples: number[] = [];
      for (let i = 0; i < 50; i++) {
        const t0 = performance.now();
        groupEventsByRun(events);
        samples.push(performance.now() - t0);
      }
      const ms = median(samples);
      console.log(
        `groupEventsByRun @${n}: ${ms.toFixed(3)} ms/次  ⇒ 40 次/秒 占单核 ${(ms * 40).toFixed(1)} ms/s`,
      );
    }
  });
});

describe('N2 探针：新增字段对投影热路径的增量（应可忽略）', () => {
  it('applyEvent @20k：单事件成本（对照 3344e34 的 0.2µs/事件，预算 <10µs）', () => {
    let s = initConversation('b');
    const seed = mkEvents(20000);
    for (const e of seed) s = applyEvent(s, e);
    const probe: AgentEvent = {
      type: EventType.MODEL_DELTA,
      data: { delta: 'x' },
      seq: null, // null seq ⇒ 不走去重短路，必然进 push 路径（正是本票新增计数的那条）
      run_id: 'r',
      step_id: 1,
      session_id: 'b',
    };
    // 批量摊薄：单次计时要跨两次 performance.now() 读表，量级会和被测函数同阶，
    // 所以每批 2000 次只读两次表（samples 是「每批的平均单次成本」）。
    const samples: number[] = [];
    for (let batch = 0; batch < 20; batch++) {
      const t0 = performance.now();
      for (let i = 0; i < 2000; i++) applyEvent(s, probe);
      samples.push(((performance.now() - t0) / 2000) * 1000);
    }
    const us = median(samples);
    console.log(`applyEvent @20000（真实 push 路径，seq=null 不去重）: ${us.toFixed(3)} µs/事件`);
    // 预算沿用 projection.perf.test.ts 的 50µs 绝对上限（健康值 0.2µs 的 250 倍余量）
    expect(us).toBeLessThan(50);
  });
});
