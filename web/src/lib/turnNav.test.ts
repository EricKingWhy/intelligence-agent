/** 轮次导航推断（#1429 打磨点 6）——压缩置灰规则的表驱动锁。
 *
 * 票面规则：只置灰 `1 .. 首个可见轮 turn_index − 1`，依据 `turn_index` +
 * `compactions[].compacted_turn_count`；带压缩前缀的 fixture 必须覆盖到。
 */

import { describe, expect, it } from 'vitest';
import type { ContextCompaction, Turn } from '../types';
import {
  compactedThrough,
  currentTurnIndex,
  deriveTurnNav,
  rowAtOffset,
  turnAnchor,
} from './turnNav';

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
      name: '同一 turn_index 落在多个渲染轮 ⇒ 药丸指向轮首（首写胜）',
      turns: [turn(1, 1), turn(2, 1), turn(3, 2)],
      compactions: [],
      want: [
        { i: 1, step: 1, off: null },
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

describe('currentTurnIndex — 阅读位置 → 当前轮（表驱动）', () => {
  /** 虚拟化行窗口（只取判定需要的 index/start/end）。 */
  const rows = (...spans: [number, number, number][]) =>
    spans.map(([index, start, end]) => ({ index, start, end }));
  const THREE = rows([0, 0, 240], [1, 240, 480], [2, 480, 720]);
  const THREE_TURNS = [turn(1, 1), turn(2, 2), turn(3, 3)];

  it('阅读位置（容器顶边）落在哪一行 → 该行的 turn_index', () => {
    expect(currentTurnIndex(THREE_TURNS, THREE, 0)).toBe(1);
    expect(currentTurnIndex(THREE_TURNS, THREE, 239)).toBe(1);
    expect(currentTurnIndex(THREE_TURNS, THREE, 240)).toBe(2);
    expect(currentTurnIndex(THREE_TURNS, THREE, 479)).toBe(2);
    expect(currentTurnIndex(THREE_TURNS, THREE, 480)).toBe(3);
    expect(currentTurnIndex(THREE_TURNS, THREE, 700)).toBe(3);
  });

  it('亚像素容差：跳转落点差零点几像素不算跨行', () => {
    expect(currentTurnIndex(THREE_TURNS, THREE, 239.4)).toBe(1);
    expect(currentTurnIndex(THREE_TURNS, THREE, 239.6)).toBe(2);
  });

  it('窗口只含 overscan 片段：offset 早于窗口首行 → 取窗口首行', () => {
    const tail = rows([1, 240, 480], [2, 480, 720]);
    expect(currentTurnIndex(THREE_TURNS, tail, 0)).toBe(2);
  });

  it('offset 超过窗口末行（尾部卡片 / 贴底）→ 取末行', () => {
    expect(currentTurnIndex(THREE_TURNS, THREE, 100000)).toBe(3);
  });

  it('无虚拟行（SSR / 空会话）→ null', () => {
    expect(rowAtOffset([], 0)).toBeNull();
    expect(currentTurnIndex(THREE_TURNS, [], 0)).toBeNull();
  });

  it('被取代 / 缺 turn_index 的行沿用最近的可导航轮（不清空高亮）', () => {
    const turns = [turn(1, 1), turn(2, 2, { superseded: true }), turn(3, null), turn(4, 4)];
    const four = rows([0, 0, 240], [1, 240, 480], [2, 480, 720], [3, 720, 960]);
    expect(currentTurnIndex(turns, four, 240)).toBe(1);
    expect(currentTurnIndex(turns, four, 480)).toBe(1);
    expect(currentTurnIndex(turns, four, 720)).toBe(4);
  });

  it('阅读带（V2 rootMargin 32%）：下一轮行顶进入带内即换轮，且取带内最靠上者', () => {
    // 带 100px：第 2 行顶（240）还在带外（300+100=400 之外？240 < 300 ⇒ 已越过，不是候选）。
    expect(currentTurnIndex(THREE_TURNS, THREE, 300, 100)).toBe(2);
    // 带 200px：第 3 行顶 480 落在 [300, 500] 带内 ⇒ 提前换到第 3 轮（V2 同口径）。
    expect(currentTurnIndex(THREE_TURNS, THREE, 300, 200)).toBe(3);
    // 带内有两行时取最靠上的那一行，不是最后一行。
    const short = rows([0, 0, 240], [1, 100, 340], [2, 480, 720]);
    expect(currentTurnIndex(THREE_TURNS, short, 0, 200)).toBe(1);
  });

  it('跳转落在列表末尾（容器滚不动、目标行顶停在阅读线下方）仍算目标轮', () => {
    // 实测形状：容器顶 880、第 3 行顶 934（差 54px）、容器高 555 ⇒ 带 177.6px。
    const tail = rows([1, 423, 880], [2, 934, 1391]);
    expect(currentTurnIndex(THREE_TURNS, tail, 880, 555 * 0.32)).toBe(3);
    // 同一位置若按纯顶边口径（带 0）会停在上一轮——这正是带判据要修的情形。
    expect(currentTurnIndex(THREE_TURNS, tail, 880)).toBe(2);
  });

  it('滚到底：取最后一个可导航轮（跳转落到短末轮时也必须高亮到它）', () => {
    // 3 行、容器一屏半：第 3 行顶永远停在阅读线下方，带也够不着。
    const rows3 = rows([0, 0, 457], [1, 457, 914], [2, 914, 1371]);
    expect(currentTurnIndex(THREE_TURNS, rows3, 536, 267)).toBe(2);
    expect(currentTurnIndex(THREE_TURNS, rows3, 536, 267, true)).toBe(3);
    // 末轮被取代 / 缺 turn_index 时回退到最近的可导航轮。
    const turns = [turn(1, 1), turn(2, 2), turn(3, null, { superseded: true })];
    expect(currentTurnIndex(turns, rows3, 536, 267, true)).toBe(2);
    // 空窗口仍然是 null（SSR）。
    expect(currentTurnIndex(THREE_TURNS, [], 0, 0, true)).toBeNull();
  });

  it('行下标越界（窗口与 turns 暂时不同步）不抛错', () => {
    expect(currentTurnIndex(THREE_TURNS, rows([9, 0, 10]), 0)).toBe(3);
  });
});
