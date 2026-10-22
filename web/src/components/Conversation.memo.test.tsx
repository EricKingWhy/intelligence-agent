// @vitest-environment jsdom
/** F2（#272）：`memo(Conversation)` 命中 + 「props 变了必须重渲染」反例守卫。
 *
 *  **为什么单独一个文件、且要 jsdom**：判的是「**同一实例**在父级提交时有没有重渲染」，
 *  `memo` 的浅比较只在客户端渲染器里发生；默认车道的 `renderToString` 每次都是全新渲染。
 *  沿用 F1（#270）同款做法（文件级 `@vitest-environment`，不动既有 SSR 文件）。
 *
 *  **渲染计数器为什么挂在 `useVirtualizer` 上**：它是 `Conversation` 里**唯一无条件执行**
 *  的外部调用（函数体第一段、两处 early-return 之前）⇒ 调用次数 = 组件渲染次数。
 *  换用 `emptyChildTurnIndex` / `latestEditableTurn` 不行——它们在 `useMemo` 里、依赖
 *  `turns`，父级提交时本来就**不该**重算，量不到"组件渲染了几次"。这里把整个虚拟化模块
 *  换成计数用的最小假实现（本文件只测"渲染了几次"，不需要真实虚拟化）。
 *
 *  ── 为什么没有 `areEqual`（票面 §必做 1 要求"先读 App.tsx 再决定"）────────────────
 *  逐项核对结论：Conversation 的**每一个** prop 引用都天然稳定，不需要"按内容比较"的
 *  自定义比较函数——
 *    - `conversation`：每次投影提交换引用（顶层浅克隆）——那是**应该**重渲染的信号；
 *    - `loadingHistory`：`useState(false)` 布尔（`useSession.ts:344`）；
 *    - `density` / `jumpRequest` / `goneApprovalIds`：`useState`（App.tsx:217 / :303 / :321）。
 *      特别地，票面担心的 `goneApprovalIds`（`ReadonlySet`）**是 useState 持有的**
 *      ⇒ 引用天然稳定，"每次渲染新建 Set"的场景不存在；
 *    - 一组回调：全部 `useCallback`（App.tsx:263-264 的注释就是为 memo 写的）；
 *    - `disclosure` / `reasoningDisclosure`：F1（#270）已给出引用稳定契约
 *      （`lib/disclosure.ts:123-126` / `:177-181`，带 F1 的用例）。
 *  不写比较函数 ⇒ 票面 Risks 第 1 条点名的两个坑（漏比 `jumpRequest.nonce` / 漏比
 *  `goneApprovalIds`）从类型上就不存在。但"稳定"不等于"该吞掉更新"，所以下面第 3 节
 *  逐条钉住**每一个会变的 prop 变化时都必须重渲染**。
 */
import { useCallback, useState } from 'react';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@tanstack/react-virtual', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-virtual')>();
  return {
    ...actual,
    // 假实现必须**自包含**（`vi.mock` 工厂被提升，不能引用模块级变量）。本文件不测虚拟化，
    // 只借它的调用次数当"Conversation 渲染了几次"的计数器。
    // 但 F8（#1429）要数**子组件**的渲染次数，所以这里把窗口内的行全部吐出来
    // （真实虚拟化在 jsdom 下没有滚动容器 ⇒ 恒空树，数不到任何 TurnView）。
    useVirtualizer: vi.fn((opts: { count: number }) => ({
      getVirtualItems: () =>
        Array.from({ length: opts.count }, (_, i) => ({
          index: i,
          key: i,
          start: i * 240,
          end: i * 240 + 240,
          size: 240,
          lane: 0,
        })),
      getTotalSize: () => opts.count * 240,
      measureElement: () => {},
      scrollToIndex: () => {},
    })),
  };
});

// F8（#1429）：`formatShortDuration` 是「组头渲染了几次」的唯一无条件调用
// （`GroupDuration` 每次渲染算一次跨度）；`formatDuration` 同理服务 `TurnView`。
// 两者都透传真实实现，只是记下调用参数——断言数的是渲染次数，不是格式。
vi.mock('../lib/format', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/format')>();
  return {
    ...actual,
    formatDuration: vi.fn(actual.formatDuration),
    formatShortDuration: vi.fn(actual.formatShortDuration),
  };
});

import { useVirtualizer } from '@tanstack/react-virtual';
import { formatDuration, formatShortDuration } from '../lib/format';
import { applyEvent, initConversation, projectHistory } from '../lib/projection';
import { useDisclosure, useReasoningDisclosure } from '../lib/disclosure';
import type { TraceDensity } from '../lib/density';
import { EventType } from '../types';
import type { AgentEvent, ConversationState, ToolCall } from '../types';
import { Conversation } from './Conversation';

const virtSpy = vi.mocked(useVirtualizer);
const durSpy = vi.mocked(formatDuration);
const shortSpy = vi.mocked(formatShortDuration);

const CONV: ConversationState = initConversation('s1');

// ── 宿主：照抄 App.tsx 的 props 契约（真 hook、真 useState/useCallback）──

function CenterHost({ conversation, tick }: { conversation: ConversationState | null; tick: number }) {
  const [density, setDensity] = useState<TraceDensity>('balanced');
  const [jumpRequest, setJumpRequest] = useState<{ key: string; nonce: number } | null>(null);
  const [goneApprovalIds, setGone] = useState<ReadonlySet<string>>(() => new Set<string>());

  const disclosure = useDisclosure('s1');
  const reasoningDisclosure = useReasoningDisclosure('s1', density);

  const onPresetTask = useCallback(() => {}, []);
  const onFocusTool = useCallback((_t: ToolCall) => {}, []);
  const onOpenSession = useCallback((_id: string) => {}, []);
  const onInspectChild = useCallback((_c: { childSessionId: string; target: string }) => {}, []);
  const onFork = useCallback((_seq: number) => {}, []);
  const onEditTurn = useCallback((_seq: number, _content: string) => {}, []);
  const onApprovalGone = useCallback((_id: string) => {}, []);

  return (
    <section data-host-tick={tick}>
      {/* 三条"会变的 prop"的驱动按钮：用来证明稳定化没有把更新一起吞掉。 */}
      <button data-act="jump" onClick={() => setJumpRequest((cur) => ({ key: 'tool:t1', nonce: (cur?.nonce ?? 0) + 1 }))} />
      <button data-act="gone" onClick={() => setGone(new Set(['a1']))} />
      <button data-act="density" onClick={() => setDensity((d) => (d === 'balanced' ? 'compact' : 'balanced'))} />
      <Conversation
        conversation={conversation}
        loadingHistory={false}
        density={density}
        disclosure={disclosure}
        reasoningDisclosure={reasoningDisclosure}
        jumpRequest={jumpRequest}
        onPresetTask={onPresetTask}
        onFocusTool={onFocusTool}
        onOpenSession={onOpenSession}
        onInspectChild={onInspectChild}
        onFork={onFork}
        onEditTurn={onEditTurn}
        goneApprovalIds={goneApprovalIds}
        onApprovalGone={onApprovalGone}
      />
    </section>
  );
}

// ── 渲染夹具 ──

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(ui: React.ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

const renders = () => virtSpy.mock.calls.length;
const hostTick = (): string =>
  container!.querySelector('[data-host-tick]')!.getAttribute('data-host-tick')!;
const click = (act$: string) =>
  act(() => (container!.querySelector(`[data-act="${act$}"]`) as HTMLElement).click());

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  virtSpy.mockClear();
});

// ── AC1 ──

describe('F2 — AC1 `memo` 包裹', () => {
  it('Conversation 是 memo 组件（导出名不变 ⇒ 上面的 import 就是证据）', () => {
    expect((Conversation as unknown as { $$typeof?: symbol }).$$typeof).toBe(Symbol.for('react.memo'));
  });
});

// ── AC3(a) 的 Conversation 侧 ──

describe('F2 — AC3(a) 与对话无关的提交不得让本组件重渲染', () => {
  it('父级 5 次无关提交，Conversation 一次都不重渲染', () => {
    render(<CenterHost conversation={CONV} tick={0} />);
    const base = renders();
    expect(base).toBe(1);

    for (let t = 1; t <= 5; t++) render(<CenterHost conversation={CONV} tick={t} />);
    expect(hostTick()).toBe('5'); // 前提：父级确实又提交了 5 次
    expect(renders()).toBe(base); // 改造前每次都 +1（本用例即红证）
  });

  it('内容真的变了就必须重渲染（`conversation` 换引用；不是"永不重渲染"）', () => {
    render(<CenterHost conversation={CONV} tick={0} />);
    const base = renders();
    render(<CenterHost conversation={initConversation('s1')} tick={1} />);
    expect(renders()).toBeGreaterThan(base);
  });
});

// ── 反例守卫：每一个会变的 prop 都必须穿透 memo ──

describe('F2 — 反例守卫（票面 Risks 点名的三个 prop 逐个钉住）', () => {
  it('`jumpRequest` 换引用（nonce 变）⇒ 必须重渲染（漏比它会让 Timeline 跳转静默失效）', () => {
    render(<CenterHost conversation={CONV} tick={0} />);
    const base = renders();
    click('jump');
    expect(renders()).toBeGreaterThan(base);
  });

  it('`goneApprovalIds` 换引用 ⇒ 必须重渲染（漏比它会让审批卡不失效）', () => {
    render(<CenterHost conversation={CONV} tick={0} />);
    const base = renders();
    click('gone');
    expect(renders()).toBeGreaterThan(base);
  });

  it('`density` 换档 ⇒ 必须重渲染（档位要流到执行链）', () => {
    render(<CenterHost conversation={CONV} tick={0} />);
    const base = renders();
    click('density');
    expect(renders()).toBeGreaterThan(base);
  });

  it('`null` ↔ 非 `null` 的 conversation 切换必须重渲染（空态 ⇄ 有内容）', () => {
    render(<CenterHost conversation={null} tick={0} />);
    const base = renders();
    render(<CenterHost conversation={CONV} tick={1} />);
    expect(renders()).toBeGreaterThan(base);
  });
});

// ── F8（#1429）：细粒度重渲染证据 ──────────────────────────────────────────
//
// 票面要求两条可测事实：
//  1. 同一 turn 追加 delta ⇒ **已完成**的 TurnView 不重渲染；
//  2. 分组开合只重渲染**被点的那一组**（其余组 memo 命中）。
//
// 计数口径：`TurnView` 每次渲染都无条件算一次 `formatDuration(started_at,
// completed_at)`（逐轮渲染计数），`GroupDuration` 每次渲染都算一次
// `formatShortDuration(span)`（逐组渲染计数）。两个 spy 都透传真实实现，
// 断言按**参数**把调用归属到具体轮 / 具体组——数的是渲染，不是格式。

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: 'run-1', step_id: null, time: null, ...partial } as AgentEvent;
}

/** 三轮会话：前两轮已收口，第三轮停在流式（可继续吃 delta）。 */
function threeTurns(): ConversationState {
  const events: AgentEvent[] = [
    ev({ type: EventType.USER_MESSAGE, seq: 1, step_id: 1, time: '2026-10-20T00:00:00Z', data: { content: '一' } }),
    ev({ type: EventType.RUN_STARTED, seq: 2, time: '2026-10-20T00:00:00Z', data: { turn_index: 1 } }),
    ev({ type: EventType.MODEL_STARTED, seq: 3, step_id: 1, data: { step: 1 } }),
    ev({ type: EventType.MODEL_COMPLETED, seq: 4, step_id: 1, data: { content: 'A' } }),
    ev({ type: EventType.RUN_COMPLETED, seq: 5, time: '2026-10-20T00:00:02Z', data: {} }),
    ev({ type: EventType.USER_MESSAGE, seq: 6, step_id: 2, time: '2026-10-20T00:01:00Z', data: { content: '二' } }),
    ev({ type: EventType.RUN_STARTED, seq: 7, time: '2026-10-20T00:01:00Z', data: { turn_index: 2 } }),
    ev({ type: EventType.MODEL_STARTED, seq: 8, step_id: 2, data: { step: 2 } }),
    ev({ type: EventType.MODEL_COMPLETED, seq: 9, step_id: 2, data: { content: 'B' } }),
    ev({ type: EventType.RUN_COMPLETED, seq: 10, time: '2026-10-20T00:01:02Z', data: {} }),
    ev({ type: EventType.USER_MESSAGE, seq: 11, step_id: 3, time: '2026-10-20T00:02:00Z', data: { content: '三' } }),
    ev({ type: EventType.RUN_STARTED, seq: 12, time: '2026-10-20T00:02:00Z', data: { turn_index: 3 } }),
    ev({ type: EventType.MODEL_STARTED, seq: 13, step_id: 3, data: { step: 3 } }),
    ev({ type: EventType.MODEL_DELTA, seq: 14, step_id: 3, data: { delta: 'C' } }),
  ];
  return projectHistory('s-memo', events);
}

/** 按 `formatDuration` 的首参（该轮 started_at）数渲染次数。 */
const rendersOfTurn = (startedAt: string) =>
  durSpy.mock.calls.filter(([s]) => s === startedAt).length;

describe('F8 — delta 只重渲染活跃轮', () => {
  it('第三轮追加 delta：前两轮（已完成）TurnView 渲染次数不增', () => {
    const conv = threeTurns();
    expect(conv.turns).toHaveLength(3);
    const [t1, t2] = conv.turns;
    expect(t1.completed_at).toBeDefined();
    expect(t2.completed_at).toBeDefined();

    render(<CenterHost conversation={conv} tick={0} />);
    const base1 = rendersOfTurn(t1.started_at!);
    const base2 = rendersOfTurn(t2.started_at!);
    const base3 = rendersOfTurn(conv.turns[2].started_at!);
    expect(base1).toBeGreaterThan(0); // 前提：这些轮真的渲染过
    expect(base2).toBeGreaterThan(0);
    expect(base3).toBeGreaterThan(0);

    const next = applyEvent(conv, ev({
      type: EventType.MODEL_DELTA, seq: 15, step_id: 3, data: { delta: 'D' },
    }));
    render(<CenterHost conversation={next} tick={1} />);

    expect(rendersOfTurn(t1.started_at!)).toBe(base1);
    expect(rendersOfTurn(t2.started_at!)).toBe(base2);
    expect(rendersOfTurn(conv.turns[2].started_at!)).toBeGreaterThan(base3);
  });
});

describe('F8 — 分组开合只重渲染被点组', () => {
  /** 一轮里两个组：3 个 bash（span 2s）+ 3 个 read（span 5s）。 */
  function twoGroups(): ConversationState {
    const T = '2026-10-20T00:00:00Z';
    const at = (s: number) => new Date(Date.parse(T) + s * 1000).toISOString();
    const tool = (id: string, name: string, s: number, e: number): ToolCall => ({
      tool_call_id: id, name, args: { command: 'ls' }, status: 'success', result: 'ok',
      started_at: at(s), completed_at: at(e),
    });
    const tools = [
      tool('b1', 'bash', 0, 1), tool('b2', 'bash', 0, 1), tool('b3', 'bash', 0, 2),
      tool('r1', 'read', 10, 12), tool('r2', 'read', 10, 14), tool('r3', 'read', 10, 15),
    ];
    const conv = initConversation('s-groups');
    return {
      ...conv,
      turns: [{
        step_id: 1,
        user_message: 'hi',
        model: { text: 'ok', status: 'done' },
        segments: [{ text: 'ok', status: 'done' }],
        tools,
        activities: tools.map((t) => ({ kind: 'tool' as const, tool_call_id: t.tool_call_id })),
        status: 'done',
        turn_index: 1,
        user_message_seq: 1,
        started_at: at(0),
        completed_at: at(16),
      }],
    };
  }

  const groupRenders = (spanMs: number) =>
    shortSpy.mock.calls.filter(([ms]) => ms === spanMs).length;

  it('点开第一组：第一组重渲染，第二组（memo 命中）不重渲染', () => {
    render(<CenterHost conversation={twoGroups()} tick={0} />);
    const heads = container!.querySelectorAll('.act-group-head');
    expect(heads).toHaveLength(2);

    const baseA = groupRenders(2000);
    const baseB = groupRenders(5000);
    expect(baseA).toBeGreaterThan(0);
    expect(baseB).toBeGreaterThan(0);

    act(() => (heads[0] as HTMLElement).click());

    expect(groupRenders(2000)).toBeGreaterThan(baseA);
    expect(groupRenders(5000)).toBe(baseB);
  });
});
