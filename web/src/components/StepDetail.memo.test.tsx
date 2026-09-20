// @vitest-environment jsdom
/** F2（#272）：`memo(StepDetail)` 命中 + 派生收敛的可执行证据 + 「变化时必须重算」反例守卫。
 *
 *  **为什么单独一个文件、且要 jsdom**：这些用例判的是「**同一实例**在父级提交后有没有
 *  重渲染 / 有没有重算派生」，`memo` 与 `useMemo` 的 bail-out 只发生在客户端渲染器的
 *  reconciliation 里。本仓默认车道的 `renderToString` 是单次渲染，memo 永不 bail out ⇒
 *  在 node 车道里不可观测。沿用 F1（#270）`Conversation.render.test.tsx` 与 N2（#271）
 *  `StepDetail.render.test.tsx` 同款：文件级 `@vitest-environment`，不动既有 SSR 文件。
 *
 *  **计数器为什么用 spy**（票面 AC3 的原话就是"用 spy"）：`allTools` / `deriveRunPulse` /
 *  `deriveAgentProfile` 都是 `lib/` 里的纯函数，`vi.mock` + 原实现透传既能计数、又不会
 *  把被测行为换成假的。不能改成"渲染期改模块级变量"——那会命中 oxlint 的
 *  `react(immutability)` / `react(globals)`（门禁要求零新增告警）。
 *
 *  **「组件重渲染」与「派生重算」是两件事，本文件分开判**：
 *    - 组件是否重渲染 → 判 **DOM 结果**（`aria-pressed` / `data-peek` 真的变了）；
 *    - 派生是否重算   → 判 spy 计数。
 *  混用会写出错用例：`panel` 变化时组件**必须**重渲染，但 `allTools` 的键（`turns`）没变
 *  ⇒ 它**不该**重算——拿它的计数当"重渲染"的代理就会把正确行为判成 bug（实测踩到）。
 *
 *  ── AC3 的口径（本票 DoD 有完整说明，此处给代码级理由）─────────────────────────
 *  票面原文「Inspector 关闭状态下**追加 delta**，`allTools` / `deriveAgentProfile` 调用
 *  次数不增长」在 N2 契约下**不可能同时成立**：
 *    - `deriveAgentProfile` 的键按票面必做 2 必须是 `eventsVersion`，而任何**真正追加
 *      成功**的事件都让 `eventsVersion` +1（`projection.ts:1171/1190`；唯一不增的路径是
 *      去重短路 `return state`，它返回**同一个 state 对象**）⇒ 必然重算；
 *    - `model/delta` 还会经 `withTurnAt → replaceTurnAt`（`projection.ts:251-254`）换掉
 *      `turns` 引用 ⇒ 连 `allTools` 也必然重算。
 *  ⇒ 按用户 2026-09-18 的裁决收窄为两条**可成立**的断言：
 *    (a) 与对话无关的提交：`memo` 命中 ⇒ 全部派生一次都不重算（第 2 节）；
 *    (b) 追加一个**不改轮次**的事件：`tools` / `pulse` 命中，`agentProfile` 按 N2 契约
 *        必然重算 —— 这一条**如实断言它涨了 1**，不假装它没涨（第 3 节）。
 *  另外配反例守卫（第 4 节）：记忆化的键是输入，不是"永不重渲染"。
 */
import { useCallback, useState } from 'react';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/projection', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/projection')>();
  return { ...actual, allTools: vi.fn(actual.allTools) };
});
vi.mock('../lib/runState', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/runState')>();
  return {
    ...actual,
    deriveRunPulse: vi.fn(actual.deriveRunPulse),
    deriveAgentProfile: vi.fn(actual.deriveAgentProfile),
  };
});

import { allTools, applyEvent, initConversation } from '../lib/projection';
import { deriveAgentProfile, deriveRunPulse } from '../lib/runState';
import { EventType } from '../types';
import type { AgentEvent, ConversationState, ToolCall } from '../types';
import { StepDetail } from './StepDetail';
import type { InspectorFocus, InspectorPanelAction } from './StepDetail';

const allToolsSpy = vi.mocked(allTools);
const pulseSpy = vi.mocked(deriveRunPulse);
const profileSpy = vi.mocked(deriveAgentProfile);

// ── 夹具 ──

/** 一个收口的 run：1 轮 · 1 个 bash 工具 · 5 条事件。 */
const SEED: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: { agent_profile: 'main' }, seq: 1, run_id: 'r1', session_id: 'f2' },
  { type: EventType.USER_MESSAGE, data: { content: 'hi', step: 1 }, seq: 2, run_id: 'r1', step_id: 1, session_id: 'f2' },
  { type: EventType.MODEL_COMPLETED, data: { content: 'ok', step: 1 }, seq: 3, run_id: 'r1', step_id: 1, session_id: 'f2' },
  { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: { command: 'ls' } }, seq: 4, run_id: 'r1', step_id: 1, session_id: 'f2' },
  { type: EventType.RUN_COMPLETED, data: {}, seq: 5, run_id: 'r1', session_id: 'f2' },
];

/** 不改轮次的事件（`noopProjection`，`projection.ts:1066`）：只进 events 日志。 */
const NOOP: AgentEvent = { type: EventType.SESSION_RESUMED, data: {}, seq: 6, session_id: 'f2' };

/** 改轮次的事件：文本增量走 `projectTextDelta → withTurnAt → replaceTurnAt`。 */
const DELTA: AgentEvent = { type: EventType.MODEL_DELTA, data: { delta: 'x' }, seq: 7, run_id: 'r1', step_id: 1, session_id: 'f2' };

const fold = (conv: ConversationState, events: AgentEvent[]): ConversationState =>
  events.reduce(applyEvent, conv);

const seed = (): ConversationState => fold(initConversation('f2'), SEED);

// ── 宿主：照抄 App.tsx 的 props 稳定性契约 ──

/** Inspector 宿主。**刻意照抄 App 的三件事**，否则测的就不是真实场景：
 *  1. `focus` / `panel` 是 `useState` 持有的对象、回调是 `useCallback`（App.tsx:265-351）
 *     —— 这正是 memo 能命中的前提；
 *  2. `focusTool` / `focusEvent` 顺手把 peek 打开（App.tsx:281-288 的 `setPanel` 分支）；
 *  3. 关闭 Inspector 用的是**祖先节点的 class**（App.tsx:920-923 的
 *     `app-regions inspector-closed`），**不是卸载**。注意票面 §Problem 把它写成
 *     "`hidden` 属性"，与实现不符（`hidden` 用在 workspace panel 与 peek 上）——
 *     结论不变（关闭 ≠ 卸载），但机制以这里为准。 */
function InspectorHost({ conversation, tick }: { conversation: ConversationState; tick: number }) {
  const [focus, setFocus] = useState<InspectorFocus>({ kind: 'run' });
  const [panel, setPanel] = useState({ pinned: false, expanded: false, width: 320, peekOpen: false });
  const [open, setOpen] = useState(true);

  const onFocusRun = useCallback(() => {
    setFocus({ kind: 'run' });
    setPanel((cur) => (cur.peekOpen ? { ...cur, peekOpen: false } : cur));
  }, []);
  const onFocusTool = useCallback((tool: ToolCall) => {
    setFocus({ kind: 'tool', tool });
    setPanel((cur) => (cur.peekOpen ? cur : { ...cur, peekOpen: true }));
  }, []);
  const onFocusEvent = useCallback((event: AgentEvent) => {
    setFocus({ kind: 'event', event });
    setPanel((cur) => (cur.peekOpen ? cur : { ...cur, peekOpen: true }));
  }, []);
  const onPanelAction = useCallback((a: InspectorPanelAction) => {
    if (a.type === 'close') {
      setOpen(false);
      return;
    }
    setPanel((cur) => {
      if (a.type === 'pin') return { ...cur, pinned: a.value };
      if (a.type === 'expand') return { ...cur, expanded: a.value };
      if (a.type === 'resize') return { ...cur, width: a.width };
      if (a.type === 'open-peek') return { ...cur, peekOpen: true };
      if (a.type === 'close-peek') return { ...cur, peekOpen: false };
      return cur;
    });
  }, []);

  return (
    <main className={`app-regions${open ? '' : ' inspector-closed'}`} data-host-tick={tick}>
      <StepDetail
        conversation={conversation}
        streaming
        focus={focus}
        onFocusRun={onFocusRun}
        onFocusTool={onFocusTool}
        onFocusEvent={onFocusEvent}
        panel={panel}
        onPanelAction={onPanelAction}
      />
    </main>
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

const hostTick = (): string =>
  container!.querySelector('[data-host-tick]')!.getAttribute('data-host-tick')!;
const counts = () => ({
  all: allToolsSpy.mock.calls.length,
  pulse: pulseSpy.mock.calls.length,
  profile: profileSpy.mock.calls.length,
});
const click = (sel: string) => act(() => (container!.querySelector(sel) as HTMLElement).click());

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  /* jsdom 的已知缺口：`Element.prototype.scrollIntoView` 不存在（不是 DOM 规范的必选实现）。
   * Timeline 的「选中项滚进视野」effect（StepDetail.tsx:1055-1057）会调到它——只补缺失，
   * 不覆盖真实实现，也不改被测逻辑。 */
  if (typeof Element.prototype.scrollIntoView !== 'function') {
    Element.prototype.scrollIntoView = () => {};
  }
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  allToolsSpy.mockClear();
  pulseSpy.mockClear();
  profileSpy.mockClear();
});

// ── AC1：结构（memo 包裹；导出名与调用点不变）──

describe('F2 — AC1 `memo` 包裹', () => {
  it('StepDetail 是 memo 组件（导出名不变 ⇒ 上面的 import 就是证据）', () => {
    expect((StepDetail as unknown as { $$typeof?: symbol }).$$typeof).toBe(Symbol.for('react.memo'));
  });

  it('收敛后 tab 计数仍与数据源同源（Timeline = 事件数，Terminal = 命令行数）', () => {
    render(<InspectorHost conversation={seed()} tick={0} />);
    const badge = (label: string) =>
      container!.querySelector(`button[aria-label="${label}"] .detail-tab-count`)?.textContent;
    expect(badge('Timeline')).toBe('5');
    expect(badge('Terminal')).toBe('1');
    expect(badge('Changes')).toBe('0');
    expect(badge('Artifacts')).toBe('0');
  });
});

// ── AC3(a)：Inspector 关闭 + 与对话无关的提交 ──

describe('F2 — AC3(a) 与对话无关的提交不得让派生重算', () => {
  it('面板关闭后仍挂载，且 5 次无关提交一次派生都不重算', () => {
    const conv = seed();
    render(<InspectorHost conversation={conv} tick={0} />);
    const before = counts();
    expect(before.all).toBe(1); // 挂载各算一次

    click('button[aria-label="关闭 Inspector"]');
    // 刻意设计：关闭只是换祖先 class，组件**不卸载**（App.tsx:920-923）。
    expect(container!.querySelector('[data-panel="inspector"]')).not.toBeNull();
    expect(container!.querySelector('.app-regions')!.className).toContain('inspector-closed');
    expect(counts()).toEqual(before); // 关闭那一拍 props 全等 ⇒ memo 已命中

    for (let t = 1; t <= 5; t++) render(<InspectorHost conversation={conv} tick={t} />);
    expect(hostTick()).toBe('5'); // 前提：父级确实又提交了 5 次
    // 结论：改造前这三项每次提交各 +1（本用例即红证）。
    expect(counts()).toEqual(before);
  });
});

// ── AC2（收窄口径）：追加一个**不改轮次**的事件 ──

describe('F2 — AC2 追加不改轮次的事件：轮次派生命中，events 派生按 N2 契约重算', () => {
  it('`tools` / `pulse` 不重算；`agentProfile` 涨 1（如实断言，不假装不涨）', () => {
    const first = seed();
    render(<InspectorHost conversation={first} tick={0} />);
    const before = counts();

    const second = fold(first, [NOOP]);
    // 前提钉住：这一帧真的"追加成功"了，但轮次一个字节都没动。
    expect(second).not.toBe(first);
    expect(second.events).toBe(first.events); // append-only 共享数组（P0-1）
    expect(second.eventsVersion).toBe(first.eventsVersion + 1);
    expect(second.turns).toBe(first.turns);

    render(<InspectorHost conversation={second} tick={1} />);
    expect(hostTick()).toBe('1');
    expect(counts().all).toBe(before.all);
    expect(counts().pulse).toBe(before.pulse);
    expect(counts().profile).toBe(before.profile + 1);
  });

  it('`pulse` 的键覆盖 run 状态：run 收口后必须重算（不是"永不重算"）', () => {
    const first = seed();
    render(<InspectorHost conversation={first} tick={0} />);
    const before = counts().pulse;
    const second = fold(first, [
      { type: EventType.RUN_STARTED, data: {}, seq: 6, run_id: 'r2', session_id: 'f2' },
    ]);
    expect(second.run_status).not.toBe(first.run_status); // 前提：run_status 真的换了
    render(<InspectorHost conversation={second} tick={1} />);
    expect(counts().pulse).toBeGreaterThan(before);
  });
});

// ── AC6：键盘导航行为不变（新用例覆盖票面点名的两件事）──
//
//  这两条是本票**唯一**能钉住 `listTargets` 的 `eventsVersion` 依赖的用例：
//  导航表若漏掉事件变化（或用陈旧长度），`↓` 会在原来的位置**静默不动**
//  （`moveSelection` 里 `next === selectedIndex` 直接 return，连回调都不发）。

describe('F2 — AC6 键盘导航（列表变化后目标更新 / 回调指向最新事件）', () => {
  /** 事件键位挂在 `<aside>` 上（StepDetail.tsx:400 `onKeyDown`），不是 window。 */
  const press = (key: string) =>
    act(() =>
      container!
        .querySelector('aside.step-detail')!
        .dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true })),
    );
  /** 选中项凭据：peek 头部渲染的是 `focus.event.type`（StepDetail.tsx:556）。 */
  const peekKind = (): string | null =>
    container!.querySelector('.detail-peek-kind')?.textContent ?? null;

  it('无选中时 ↑ 选最后一条 ⇒ 指向的是**当前**列表的最新事件', () => {
    render(<InspectorHost conversation={fold(seed(), [NOOP])} tick={0} />);
    press('ArrowUp');
    expect(peekKind()).toBe(EventType.SESSION_RESUMED); // 6 条里的第 6 条
  });

  it('追加事件后 ↓ 能走到新增的那一条（陈旧导航表会原地不动、连回调都不发）', () => {
    const first = seed();
    render(<InspectorHost conversation={first} tick={0} />);
    press('ArrowUp');
    expect(peekKind()).toBe(EventType.RUN_COMPLETED); // 5 条里的第 5 条

    const second = fold(first, [NOOP]);
    expect(second.eventsVersion).toBe(first.eventsVersion + 1);
    render(<InspectorHost conversation={second} tick={1} />);

    press('ArrowDown');
    // 旧表长度 5 ⇒ `min(4, 4) = 4 === current` ⇒ 早退，peek 仍是 RUN_COMPLETED。
    expect(peekKind()).toBe(EventType.SESSION_RESUMED);
  });
});

// ── 反例守卫：输入变了必须重算 / 交互必须仍有反应 ──

describe('F2 — 反例守卫（记忆化的键是输入，不是"永不重渲染"）', () => {
  it('text delta 换掉 turns 引用 ⇒ `allTools` 必须重算（陈旧派生是 N2 那类缺陷）', () => {
    const first = seed();
    render(<InspectorHost conversation={first} tick={0} />);
    const before = counts().all;
    const second = fold(first, [DELTA]);
    expect(second.turns).not.toBe(first.turns); // 前提：COW 真的换了引用
    render(<InspectorHost conversation={second} tick={1} />);
    expect(counts().all).toBeGreaterThan(before);
  });

  it('同一内容的**新对象**也必须重渲染（memo 不能"按内容"吞掉提交）', () => {
    const conv = seed();
    render(<InspectorHost conversation={conv} tick={0} />);
    const before = counts().all;
    render(<InspectorHost conversation={fold(initConversation('f2'), SEED)} tick={1} />);
    expect(counts().all).toBeGreaterThan(before);
  });

  it('点 Timeline 行：`focus` 换引用 ⇒ 重渲染并渲染出选中详情；轮次派生不重算', () => {
    render(<InspectorHost conversation={seed()} tick={0} />);
    const before = counts();
    expect(container!.querySelector('.detail-peek')).toBeNull(); // 未选中 ⇒ 无详情区

    click('button.timeline-row');

    // 「组件确实重渲染了」的证据是 DOM 结果（而不是派生计数——见文件头那段）。
    const peek = container!.querySelector('.detail-peek');
    expect(peek).not.toBeNull();
    expect(peek!.getAttribute('data-peek')).toBe('on'); // 详情真的可见（"点了有反应"）
    expect(peek!.textContent).toContain('选中项详情');
    // 而轮次派生一次都没重算：`focus` 不是它们的键（这是收窄 AC3 想要的那层收益）。
    expect(counts()).toEqual(before);
  });

  it('面板动作换 `panel` 引用 ⇒ 重渲染并反映到 DOM；轮次派生不重算', () => {
    render(<InspectorHost conversation={seed()} tick={0} />);
    const before = counts();
    const pin = () => container!.querySelector('button[aria-label="钉住"]')!;
    expect(pin().getAttribute('aria-pressed')).toBe('false');

    click('button[aria-label="钉住"]');

    expect(pin().getAttribute('aria-pressed')).toBe('true'); // 机制：props 变了，memo 必须放行
    expect(counts()).toEqual(before);
  });
});
