// @vitest-environment jsdom
/** F4（#273）红证 + 门控行为。判的是「面板**关闭**时要不要构建候选表」。
 *
 * ── 为什么必须渲染**真 App** ─────────────────────────────────────────────
 * `paletteItems` 是 App 内部的 `useMemo`（`App.tsx:743-901`）。任何「把这十几行抄进
 * 测试宿主」的写法都只测到副本，证明不了真实现。故本文件只 mock 两处：
 *   1. 数据 hook（`useSession` / `useProjects`）—— 换成可控夹具，用来**驱动投影提交**；
 *   2. `CommandPalette` 的**转发包装** —— 真组件照常渲染，仅额外捕获 `items` prop。
 * 其余（App 本体、memo 构建体、键盘监听、Radix 浮层）全走真实代码路径。
 *
 * ── 观测点为什么是 `summarizeEvent` 的 spy ────────────────────────────────
 * 它是该 memo 里**唯一**随事件条数放大的调用（`conversation.events.slice(-100)` 逐条
 * summarizeEvent），所以「调用次数」直接等于「候选表构建次数 × 100（= 窗口内事件数）」。
 * `web/src` 全仓它有**两处**调用点（grep 实证）：App.tsx:883（本文件的观测对象）与
 * StepDetail.tsx:1647（时间线每帧对全部事件各调一次）。后者由 App.tsx:1123 无条件渲染，
 * 会把计数污染成「100 + 事件总数」——实测 1125 与每次提交递增 1 都源于此。
 * 因此本文件把 StepDetail 换成空壳（它是 App 的唯一引用方），把观测面收敛到单点。
 *
 * ── 驱动方式为什么是「换 conversation 引用」 ───────────────────────────────
 * 真实场景是流式期间每秒约 40 次投影提交（`useSession.ts` 合帧窗口 24ms），每次都换
 * `conversation` 顶层引用（`projection.ts` 浅克隆）。本文件用 `commit()` 复刻同一机制，
 * 而不是点按钮绕路——`useMemo` 本就只在依赖换引用时重算，点按钮改 `inspectorOpen`
 * 虽然也能触发，但那是另一种依赖，证明力弱。
 *
 * ── 两条硬约束（改动时不得破坏）────────────────────────────────────────
 *   R1 打开**那一刻**候选表必须是最新的 ⇒ 打开态必须进依赖数组（不得靠冻结快照）；
 *   R3 关闭态必须返回**同一个引用** ⇒ 不得用 `[]` 字面量（会让下游 memo 恒 miss，
 *      把 F1/F2 的成果抵消掉）。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AgentEvent, ConversationState } from './types';
import { EventType } from './types';

// ── 观测点：summarizeEvent 调用计数（工厂被提升，只能用 vi.hoisted 传参）──
const spy = vi.hoisted(() => ({ calls: 0 }));
vi.mock('./lib/projection', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/projection')>();
  return {
    ...actual,
    summarizeEvent: (e: AgentEvent) => {
      spy.calls += 1;
      return actual.summarizeEvent(e);
    },
  };
});

// ── 观测点：每次渲染拿到的 items prop（关闭态引用同一性 + 打开态 golden 都看它）──
const cap = vi.hoisted(() => ({ props: [] as { open: boolean; items: { id: string; group: string }[] }[] }));
vi.mock('./components/CommandPalette', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./components/CommandPalette')>();
  return {
    ...actual,
    CommandPalette: (props: Parameters<typeof actual.CommandPalette>[0]) => {
      cap.props.push(props as never);
      return actual.CommandPalette(props);
    },
  };
});

// ── 隔离第二调用点：`StepDetail.tsx:1647` 也调 summarizeEvent ────────────────
// App.tsx:1123 无条件渲染 StepDetail，它的时间线每帧对**全部**事件调一次
// summarizeEvent——实测该来源为「事件总数」量级（123、124、…），会把本文件的
// 计数污染成 100+events。本文件只判 paletteItems，故把 StepDetail 换成空壳
// （它只被 App 引用，见 grep）。这是**观测点隔离**，不是放宽断言：
// AC1 的期望值因此从「≈123」收紧为**严格 0**。
vi.mock('./components/StepDetail', () => ({
  StepDetail: () => null,
}));

// ── 可控数据层：会话夹具（换引用 = 一次投影提交）、项目恒空 ──
const H = vi.hoisted(() => ({
  conv: null as ConversationState | null,
  set: null as unknown as (c: ConversationState) => void,
}));

const NOOP = () => {};

vi.mock('./hooks/useSession', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useSession')>();
  const React = await import('react');
  return {
    ...actual,
    useSession: () => {
      const [conv, setConv] = React.useState(H.conv);
      H.set = setConv;
      return {
        sessions: [],
        selectedId: null,
        conversation: conv,
        loadingHistory: false,
        streaming: false,
        reconnecting: false,
        error: null,
        sessionsError: null,
        titlesById: {},
        recoverState: { phase: 'idle' },
        selectSession: NOOP,
        submitTask: NOOP,
        sendMessage: NOOP,
        cancelStream: NOOP,
        removeSession: NOOP,
        setArchived: NOOP,
        recover: NOOP,
        refreshSessions: NOOP,
        changeModel: NOOP,
        fork: NOOP,
        sendSteer: NOOP,
        flushQueue: NOOP,
        cancelItem: NOOP,
      };
    },
  };
});

vi.mock('./hooks/useProjects', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./hooks/useProjects')>();
  return {
    ...actual,
    useProjects: () => ({
      projects: [],
      loadError: null,
      refresh: NOOP,
      // App 只把 actions 透传给子组件；测试不触发项目动作，用 Proxy 兜住任意方法名。
      actions: new Proxy({}, { get: () => NOOP }) as never,
    }),
  };
});

import { applyEvent, initConversation } from './lib/projection';
import App from './App';

// ── 夹具 ─────────────────────────────────────────────────────────────────
/** 120 条真实事件的会话 ⇒ `events.slice(-100)` 恒满窗，关闭态一次提交 = 100 次 summarizeEvent。 */
const EVENT_COUNT = 120;

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

function buildConversation(events: number): ConversationState {
  let s = applyEvent(initConversation('s1'), ev({ type: EventType.RUN_STARTED, seq: 1 }));
  s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1, seq: 2 }));
  for (let i = 0; i < events; i += 1) {
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'x' }, step_id: 1, seq: 3 + i }));
  }
  return s;
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;
let eventBudget = EVENT_COUNT;

async function mount(): Promise<void> {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root!.render(<App />);
  });
}

/** 一次投影提交：换 `conversation` 引用（与 `useSession` 合帧提交同机制）。 */
async function commit(): Promise<void> {
  eventBudget += 1;
  H.conv = buildConversation(eventBudget);
  await act(async () => {
    H.set(H.conv!);
  });
}

/** 追加一条事件（用于 AC2「关闭 → 追加 → 打开」）。返回新的窗口内最新事件 id。 */
async function commitNewEvent(): Promise<string> {
  const base = H.conv!;
  const next = applyEvent(base, ev({
    type: EventType.MODEL_DELTA, data: { delta: 'new' }, step_id: 1,
    seq: (base.events.at(-1)?.seq ?? 0) + 1,
  }));
  H.conv = next;
  await act(async () => {
    H.set(next);
  });
  return `event-${next.events.length - 1}`;
}

function lastProps(): { open: boolean; items: { id: string; group: string }[] } {
  return cap.props[cap.props.length - 1];
}

/** 真键盘路径打开面板（App 自身的 window keydown 监听 + isPaletteShortcut）。 */
async function openPalette(): Promise<void> {
  await act(async () => {
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'k', ctrlKey: true, bubbles: true }));
  });
}

beforeEach(() => {
  // createRoot 的测试必须显式声明 act 环境，否则 act() 退化成"只警告不刷新"。
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom 不实现这三个（theme.ts:15 读 matchMedia；Radix/虚拟列表要 ResizeObserver；
  // 贴底滚动要 scrollIntoView）。缺哪个就在挂载时报错，故一次补齐。
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class {
    observe = NOOP; unobserve = NOOP; disconnect = NOOP;
  });
  Element.prototype.scrollIntoView = NOOP;
  spy.calls = 0;
  cap.props.length = 0;
  eventBudget = EVENT_COUNT;
  H.conv = buildConversation(EVENT_COUNT);
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      const body = url.includes('/api/capabilities') ? [] : {};
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }),
  );
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  if (container) container.remove();
  root = null;
  container = null;
  vi.unstubAllGlobals();
});

// ── AC1 / R3：关闭态不构建，且返回同一引用 ─────────────────────────────────
describe('F4 AC1/R3 — 面板关闭时的行为', () => {
  it('关闭态连续 5 次投影提交 ⇒ summarizeEvent 调用次数为 0', async () => {
    await mount();
    spy.calls = 0; // 挂载那一拍不计入（打开态未建立，见下一条用例）
    const perCommit: number[] = [];
    for (let i = 0; i < 5; i += 1) {
      await commit();
      perCommit.push(spy.calls);
      spy.calls = 0;
    }
    // 逐次断言（而非求和）——失败时能直接看出是「哪几拍在构建」。
    expect(perCommit).toEqual([0, 0, 0, 0, 0]);
  });

  it('关闭态各次渲染拿到的 items 是同一个引用（非 `[]` 字面量）', async () => {
    await mount();
    for (let i = 0; i < 3; i += 1) await commit();
    const closed = cap.props.filter((p) => !p.open).map((p) => p.items);
    expect(closed.length).toBeGreaterThanOrEqual(3);
    for (const items of closed) expect(items).toBe(closed[0]);
  });
});

// ── AC2：打开那一刻必须最新 ───────────────────────────────────────────────
describe('F4 AC2 — 打开那一刻候选表是最新的', () => {
  it('关闭期间追加的事件，在打开后出现在候选表里', async () => {
    await mount();
    const newId = await commitNewEvent();
    await openPalette();
    expect(lastProps().open).toBe(true);
    expect(lastProps().items.map((i) => i.id)).toContain(newId);
  });

  it('已打开时每拍都重建，且当场就能搜到刚追加的事件（不是打开瞬间的冻结快照）', async () => {
    await mount();
    await openPalette();
    spy.calls = 0;
    const newId = await commitNewEvent();
    expect(spy.calls).toBeGreaterThan(0);
    expect(lastProps().items.map((i) => i.id)).toContain(newId);
  });
});

// ── AC4：打开态候选表 golden（id + 顺序 + 分组）────────────────────────────
/** 改造前实测（2026-09-19，本文件在改造**前**跑出的打开态 items，共 110 项）——
 *  门控**不得**改变这张表。事件区是 `events.slice(-100).reverse()`：窗口末 100 条、
 *  最新在前，故 id 恰为 `event-121` 递减到 `event-22`（夹具共 122 条事件）。
 *  这里写成「显式前 10 项 + 公式化事件区」，是为了让守卫独立于实现，
 *  任何窗口大小 / 排序 / 分组的偏离都会让它失败。 */
const GOLDEN_OPEN_ITEMS: [string, string][] = [
  ['toggle-inspector', 'actions'],
  ['toggle-inspector-fullpage', 'actions'],
  ['jump-latest', 'actions'],
  ['toggle-theme', 'actions'],
  ['focus-composer', 'actions'],
  ['manage-memories', 'actions'],
  ['density-compact', 'density'],
  ['density-balanced', 'density'],
  ['density-detailed', 'density'],
  ['density-raw', 'density'],
  ...Array.from(
    { length: 100 },
    (_, i): [string, string] => [`event-${121 - i}`, 'events'],
  ),
];

describe('F4 AC4 — 打开态候选表 golden', () => {
  it('id / 顺序 / 分组与改造前逐项一致', async () => {
    await mount();
    await openPalette();
    const got = lastProps().items.map((i) => [i.id, i.group]);
    expect(got).toEqual(GOLDEN_OPEN_ITEMS);
  });
});

// ── AC5：键盘行为不变（在真 Radix 浮层上按真键）────────────────────────────
describe('F4 AC5 — 键盘导航与执行', () => {
  it('↓ 移动到第二项、Enter 执行它并关闭浮层', async () => {
    await mount();
    const before = container!.querySelector('.app-regions')!.className;
    await openPalette();
    const input = document.querySelector('.palette-input')!;
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
    });
    const active = document.querySelector('.palette-item.active .palette-item-label');
    expect(active?.textContent).toBeTruthy();
    await act(async () => {
      input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    expect(document.querySelector('.palette-content')).toBeNull();
    expect(container!.querySelector('.app-regions')!.className).not.toBe(before);
  });
});

