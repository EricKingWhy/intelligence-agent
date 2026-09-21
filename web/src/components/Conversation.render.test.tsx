// @vitest-environment jsdom
/** F1（#270）必做 2/3：memo 命中（AC3/AC4）+ 已完成段 renderMarkdown 调用次数（AC5）
 *  + 「稳定化不得变成点了没反应」（AC8 的可自动化代理）。
 *
 *  为什么单独一个文件、且要 jsdom：这些用例判的都是「**同一实例**在父级提交时有没有
 *  重渲染」，这需要真实客户端渲染器做状态更新（reconciliation 才会跑 memo 的浅比较）。
 *  本仓默认车道的 `renderToStaticMarkup` 是单次渲染，memo 永不 bail out ⇒ 在 node 车道
 *  里不可观测。原有的 Conversation.test.tsx 是 SSR 契约测试，不动它的环境，这里用
 *  文件级 `@vitest-environment` 新开一条。
 *
 *  两个计数器都不能用「渲染期改模块级变量」实现——那会命中 oxlint 的
 *  `react(immutability)` / `react(globals)`（AC6 要求零新增告警）。所以：
 *  - 「父级确实提交了」的证据 = 传进去的 `tick` prop 落到了 DOM 上（`data-host-tick`）；
 *  - 「TurnView / ToolCard 渲染了几次」= 对 `formatDuration` 打桩计数——两个组件的函数体
 *    里都**无条件**调用它，它就是一分忠实的渲染计数器；两侧用不同的时间戳对，
 *    就能把「TurnView 渲染」与「ToolCard 渲染」分开计数。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/markdown', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/markdown')>();
  return { ...actual, renderMarkdown: vi.fn(actual.renderMarkdown) };
});
vi.mock('../lib/format', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/format')>();
  return { ...actual, formatDuration: vi.fn(actual.formatDuration) };
});

import { formatDuration } from '../lib/format';
import { renderMarkdown } from '../lib/markdown';
import { useDisclosure } from '../lib/disclosure';
import type { Turn } from '../types';
import { TurnView } from './Conversation';

const durationSpy = vi.mocked(formatDuration);
const mdSpy = vi.mocked(renderMarkdown);

// ── 夹具 ──

/** TurnView 的父级：持**真实** useDisclosure（缺陷的源头）。`tick` 只用于从外部驱动
 *  重渲染并留下落地证据——bump 它等于流式期间的一次合帧提交（24ms 一拍）。 */
function TurnHost({ turn, model = 'model-a', tick }: { turn: Turn; model?: string; tick: number }) {
  const disclosure = useDisclosure('session-1');
  return (
    <>
      <span data-host-tick={tick} />
      <TurnView turn={turn} model={model} density="balanced" disclosure={disclosure} />
    </>
  );
}

// 两侧时间戳刻意不同：用来把 TurnView 与 ToolCard 的 formatDuration 调用分开计数。
const TURN_START = '2026-09-18T00:00:00.000Z';
const TURN_DONE = '2026-09-18T00:00:01.000Z';
const TOOL_START = '2026-09-18T00:00:05.000Z';
const TOOL_DONE = '2026-09-18T00:00:09.000Z';

const ANSWER = '**加粗** 与 `code`';

/** 一个已完成轮：单个 done 的 model 段（走 renderMarkdown 的路径）。 */
const MODEL_TURN: Turn = {
  step_id: 1,
  user_message: 'hi',
  model: { text: ANSWER, status: 'done' },
  segments: [{ text: ANSWER, status: 'done' }],
  tools: [],
  activities: [{ kind: 'model', index: 0 }],
  status: 'done',
  turn_index: 1,
  user_message_seq: 1,
  started_at: TURN_START,
  completed_at: TURN_DONE,
};

/** 同一形状、**不同内容**——用来证明记忆化键是内容而不是「永不重渲染」。 */
const MODEL_TURN_B: Turn = {
  ...MODEL_TURN,
  model: { text: '另一段 **文本**', status: 'done' },
  segments: [{ text: '另一段 **文本**', status: 'done' }],
};

/** 一个含工具节点的已完成轮。 */
const TOOL_TURN: Turn = {
  ...MODEL_TURN,
  step_id: 2,
  segments: [],
  tools: [
    {
      tool_call_id: 'c1',
      name: 'bash',
      args: { command: 'ls' },
      status: 'success',
      result: { ok: true, data: { exit_code: 0 } },
      started_at: TOOL_START,
      completed_at: TOOL_DONE,
    },
  ],
  activities: [{ kind: 'tool', tool_call_id: 'c1' }],
};

// ── 渲染夹具 ──

let container: HTMLDivElement | null = null;
let root: Root | null = null;

/** 同一条 root 上的一次提交（首次即挂载）。 */
function render(ui: ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

/** 父级确实提交过的证据（DOM 上的 tick）。 */
const hostTick = (): string =>
  container!.querySelector('[data-host-tick]')!.getAttribute('data-host-tick')!;

/** TurnView 的渲染次数 = 以轮次时间戳调用的 formatDuration 次数。 */
const turnRenders = (): number => durationSpy.mock.calls.filter((c) => c[0] === TURN_START).length;
/** ToolCard 的渲染次数 = 以工具时间戳调用的 formatDuration 次数。 */
const toolRenders = (): number => durationSpy.mock.calls.filter((c) => c[0] === TOOL_START).length;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  durationSpy.mockClear();
  mdSpy.mockClear();
});

// ── AC3 ──

describe('F1 — memo(TurnView) 命中（AC3）', () => {
  it('父级无关状态变化时只重渲染父级，TurnView 一次都不重渲染', () => {
    render(<TurnHost turn={MODEL_TURN} tick={0} />);
    const baseTurns = turnRenders();
    // 挂载只渲染一次：disclosure 若引用不稳定，挂载后的 sessionKey effect 会立刻
    // 触发第二次 TurnView 渲染（改造前 baseTurns = 2，这条断言本身就是红证的一部分）。
    expect(baseTurns).toBe(1);
    for (let tick = 1; tick <= 4; tick++) render(<TurnHost turn={MODEL_TURN} tick={tick} />);
    // 前提：父级确实又提交了 4 次（否则下面的断言是空断言）
    expect(hostTick()).toBe('4');
    // 结论：4 次父级提交，TurnView 一次都没重跑 deriveChain / markdown
    expect(turnRenders()).toBe(baseTurns);
  });
});

// ── AC4 ──

describe('F1 — memo(ToolCard) 命中（AC4，onCycleLevel 稳定）', () => {
  it('只改与工具卡无关的 prop 时，工具卡不重渲染', () => {
    render(<TurnHost turn={TOOL_TURN} model="model-a" tick={0} />);
    const base = toolRenders();
    expect(base).toBe(1);
    // 改 model ⇒ TurnView **必然**重渲染（memo 浅比较不等），但 ToolCard 的
    // props（tool / density / level / onCycleLevel / sessionId）一个都没变。
    render(<TurnHost turn={TOOL_TURN} model="model-b" tick={1} />);
    expect(turnRenders()).toBeGreaterThan(1); // 前提成立：TurnView 真的重渲染了
    expect(toolRenders()).toBe(base);
  });
});

// ── AC8 的可自动化代理：稳定化不得把交互变成「点了没反应」 ──

describe('F1 — 稳定化不得变成「点了没反应」（AC8 代理）', () => {
  it('点击工具行后 `disclosure` 必须换引用，否则新档位到不了 ToolCard', () => {
    render(<TurnHost turn={TOOL_TURN} tick={0} />);
    const row = (): Element | null => container!.querySelector('button.act-node');
    expect(row()!.getAttribute('aria-level')).toBe('0'); // balanced 默认 L0
    const baseTurns = turnRenders();

    act(() => {
      // 走真实点击链路：ToolCard.handleRowClick → onCycleLevel → TurnView.cycleLevel
      // → disclosure.setLevel（与手工冒烟点的那一下完全同一条路）。
      (row() as HTMLElement).click();
    });

    /* 这一条是本票最容易踩的坑：把 hook 返回值做成「身份永不改变」的稳定容器，
     * 就会让 override 变化时 memo(TurnView) 也 bail out——levelFor 在 TurnView 自己的
     * 渲染体里被调用，TurnView 不重渲染 ⇒ 新的 level 永远流不到 ToolCard，
     * 屏幕上看就是「点了没反应」（票面 Risks 点名的静默 bug 的另一种形态）。
     * 实测红证：A 方案下本用例断言 `expected 1 to be greater than 1`。
     * 断言分两截：先证明确实重渲染了（机制），再证明 DOM 上的档位真的变了（结果）。 */
    expect(turnRenders()).toBeGreaterThan(baseTurns);
    expect(row()!.getAttribute('aria-level')).toBe('1'); // L0 → L1
  });
});

// ── AC5 ──

describe('F1 — 已完成段 markdown 记忆化（AC5）', () => {
  it('同一段内容被 N 次父级提交触发时，renderMarkdown 只跑一次', () => {
    render(<TurnHost turn={MODEL_TURN} tick={0} />);
    expect(mdSpy).toHaveBeenCalledTimes(1);
    for (let tick = 1; tick <= 5; tick++) render(<TurnHost turn={MODEL_TURN} tick={tick} />);
    expect(hostTick()).toBe('5'); // 前提：父级提交了 5 次
    expect(mdSpy).toHaveBeenCalledTimes(1);
  });

  it('TurnView 被迫重渲染时，内容未变的段不再重解析（与 AC3 解耦）', () => {
    render(<TurnHost turn={MODEL_TURN} model="model-a" tick={0} />);
    const base = mdSpy.mock.calls.length;
    render(<TurnHost turn={MODEL_TURN} model="model-b" tick={1} />); // 只改 model ⇒ TurnView 必重渲染
    expect(hostTick()).toBe('1');
    expect(turnRenders()).toBeGreaterThan(1);
    expect(mdSpy.mock.calls.length).toBe(base);
  });

  it('内容变了必须重新解析（键是内容，不是「永不重渲染」）', () => {
    render(<TurnHost turn={MODEL_TURN} tick={0} />);
    const base = mdSpy.mock.calls.length;
    render(<TurnHost turn={MODEL_TURN_B} tick={1} />);
    expect(mdSpy.mock.calls.length).toBe(base + 1);
  });
});
