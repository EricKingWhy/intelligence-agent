/** 轮次导航推断（#1429 打磨点 6）——压缩置灰规则的表驱动锁。
 *
 * 票面规则：只置灰 `1 .. 首个可见轮 turn_index − 1`，依据 `turn_index` +
 * `compactions[].compacted_turn_count`；带压缩前缀的 fixture 必须覆盖到。
 */

import { describe, expect, it } from 'vitest';
import type { ContextCompaction, Turn } from '../types';
import { compactedThrough, deriveTurnNav, turnAnchor } from './turnNav';

const turn = (stepId: number, turnIndex: number | null, over: Partial<Turn> = {}): Turn => ({
  step_id: stepId,
  user_message: 'hi',
  model: { text: '', status: 'done' },
  segments: [],
  tools: [],
  activities: [],
  status: 'done',
  turn_index: turnIndex,
  user_message_seq: stepId,
  ...over,
});

const compact = (n: number): ContextCompaction => ({
  compacted_turn_count: n,
  summary_message_count: 1,
  token_estimate: 100,
  fallback_used: false,
});

describe('deriveTurnNav — 1..N 与置灰（表驱动）', () => {
  const cases: {
    name: string;
    turns: Turn[];
    compactions: ContextCompaction[];
    want: { i: number; step: number | null; off: string | null }[];
  }[] = [
    {
      name: '无压缩、视图从第 1 轮开始 → 全部可跳',
      turns: [turn(1, 1), turn(2, 2), turn(3, 3)],
      compactions: [],
      want: [
        { i: 1, step: 1, off: null },
        { i: 2, step: 2, off: null },
        { i: 3, step: 3, off: null },
      ],
    },
    {
      name: '压缩前缀（compacted_turn_count=2）→ 1..2 置灰且措辞为已压缩',
      turns: [turn(3, 3), turn(4, 4)],
      compactions: [compact(2)],
      want: [
        { i: 1, step: null, off: 'compacted' },
        { i: 2, step: null, off: 'compacted' },
        { i: 3, step: 3, off: null },
        { i: 4, step: 4, off: null },
      ],
    },
    {
      name: '视图起始晚于 1 但压缩计数为 0 → 置灰但措辞不是"已压缩"',
      turns: [turn(5, 5), turn(6, 6)],
      compactions: [],
      want: [
        { i: 1, step: null, off: 'not-visible' },
        { i: 2, step: null, off: 'not-visible' },
        { i: 3, step: null, off: 'not-visible' },
        { i: 4, step: null, off: 'not-visible' },
        { i: 5, step: 5, off: null },
        { i: 6, step: 6, off: null },
      ],
    },
    {
      name: '压缩计数超过首个可见轮 → 可见轮仍可跳（能滚到的不标已压缩）',
      turns: [turn(3, 3)],
      compactions: [compact(5)],
      want: [
        { i: 1, step: null, off: 'compacted' },
        { i: 2, step: null, off: 'compacted' },
        { i: 3, step: 3, off: null },
      ],
    },
    {
      name: '多条压缩取最大 compacted_turn_count',
      turns: [turn(4, 4)],
      compactions: [compact(1), compact(3)],
      want: [
        { i: 1, step: null, off: 'compacted' },
        { i: 2, step: null, off: 'compacted' },
        { i: 3, step: null, off: 'compacted' },
        { i: 4, step: 4, off: null },
      ],
    },
    {
      name: '被取代的轮整段不渲染 ⇒ 不进导航（中段缺口跳过，不置灰）',
      turns: [turn(1, 1), turn(2, 2, { superseded: true }), turn(3, 3)],
      compactions: [],
      want: [
        { i: 1, step: 1, off: null },
        { i: 3, step: 3, off: null },
      ],
    },
    {
      name: '中段缺口（turn_index 缺号）不置灰：只有视图起点之前的前缀才灰',
      turns: [turn(1, 1), turn(3, 3)],
      compactions: [],
      want: [
        { i: 1, step: 1, off: null },
        { i: 3, step: 3, off: null },
      ],
    },
    {
      name: '被取代的轮排在视图最前 ⇒ 首个可见轮是 2，只有 1 置灰',
      turns: [turn(1, 1, { superseded: true }), turn(2, 2), turn(3, 3)],
      compactions: [compact(1)],
      want: [
        { i: 1, step: null, off: 'compacted' },
        { i: 2, step: 2, off: null },
        { i: 3, step: 3, off: null },
      ],
    },
    {
      name: 'turn_index 缺失/非正数的轮不进导航（旧版后端防御）',
      turns: [turn(1, null), turn(2, 0), turn(3, 2)],
      compactions: [],
      want: [
        { i: 1, step: null, off: 'not-visible' },
        { i: 2, step: 3, off: null },
      ],
    },
    {
      name: '无轮 → 空导航',
      turns: [],
      compactions: [compact(2)],
      want: [],
    },
  ];

  it.each(cases)('$name', ({ turns, compactions, want }) => {
    const got = deriveTurnNav(turns, compactions).map((n) => ({
      i: n.turnIndex,
      step: n.stepId,
      off: n.offReason,
    }));
    expect(got).toEqual(want);
  });
});

describe('deriveTurnNav — 文案与锚点', () => {
  it('置灰项文案区分压缩与"不在视图"', () => {
    const [compacted, notVisible, live] = deriveTurnNav(
      [turn(3, 3)],
      [compact(1)],
    );
    expect(compacted.title).toBe('第 1 轮已压缩为摘要');
    expect(notVisible.title).toBe('第 2 轮不在当前视图中');
    expect(live.title).toBe('跳到第 3 轮');
  });

  it('锚点只复制 #turn-N 文本', () => {
    expect(turnAnchor(3)).toBe('#turn-3');
  });

  it('compactedThrough：空记录 = 0，非法值忽略', () => {
    expect(compactedThrough([])).toBe(0);
    expect(compactedThrough([{ ...compact(2), compacted_turn_count: Number.NaN }])).toBe(0);
    expect(compactedThrough([compact(2), compact(7)])).toBe(7);
  });
});
