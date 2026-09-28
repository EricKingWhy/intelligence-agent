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
    useVirtualizer: vi.fn(() => ({
      getVirtualItems: () => [],
      getTotalSize: () => 0,
      measureElement: () => {},
      scrollToIndex: () => {},
    })),
  };
});

import { useVirtualizer } from '@tanstack/react-virtual';
import { initConversation } from '../lib/projection';
import { useDisclosure, useReasoningDisclosure } from '../lib/disclosure';
import type { TraceDensity } from '../lib/density';
import type { ConversationState, ToolCall } from '../types';
import { Conversation } from './Conversation';

const virtSpy = vi.mocked(useVirtualizer);

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
