// @vitest-environment jsdom
/** F3（#276）：流式贴底的「读 `scrollHeight` + 写 `scrollTop`」收进同一个 rAF 帧。
 *
 *  ## 测什么
 *  流式期间 `Conversation` 的自动贴底 effect 依赖整个 `conversation`（每次投影提交换顶层
 *  引用）⇒ 每个 delta 提交都**同步**跑一次「读 `scrollHeight`（布局）→ 写 `scrollTop`
 *  （布局失效 + 派发 scroll 事件）」。本文件把这条路径的**每帧读数**钉成计数断言：
 *  改造前 = 每次提交 1 次（一帧 N 次提交 ⇒ N 次）；改造后 = 每个 rAF 帧 1 次。
 *
 *  ## 为什么必须挂钩 `Element.prototype` 而不是 DOM 节点
 *  jsdom **没有布局**：`scrollHeight` / `clientHeight` 恒 0，`scrollTop` 写进去也不沉淀。
 *  所以「读了几次」在这里无法从节点上观察，只能替换 `Element.prototype` 上的
 *  accessor 计数（票面 §必做 2 指定的做法）。`clientHeight` 保持 jsdom 的 0 ——
 *  `nearBottom` 于是恒判「贴底」，与真实「用户就在底部跟随」的场景等价。
 *
 *  ## 为什么用真 `requestAnimationFrame` + 真 `await` 帧，而不用假定时器
 *  「同一帧」的语义靠**同步块**保证：一次提交是一次 `act()`，同一同步块里的多次提交之间
 *  不可能插入帧回调（JS 单线程）。所以「3 次提交 + 之后 await 一帧」是确定性的，
 *  不需要伪造时钟；假定时器反而会引入「rAF 是否被 toFake 覆盖」这一层与实现无关的脆弱性。
 *
 *  ## 计数器的可读性约定
 *  `reads` = `scrollHeight` 的 getter 命中次数；`writes` = `scrollTop` 的 setter 命中次数。
 *  每次断言前用 `resetCounters()` 归零，只测**被测那一段**。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import { applyEvent, initConversation } from '../lib/projection';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { Conversation } from './Conversation';

// ── 计数器：替换 Element.prototype 上的 accessor ──

const SCROLL_HEIGHT = 1234;
let reads = 0;
let writes = 0;
let lastWrittenTop: number | null = null;
let scrollTopValue = 0;

function installScrollCounters(): void {
  Object.defineProperty(Element.prototype, 'scrollHeight', {
    configurable: true,
    get() {
      reads += 1;
      return SCROLL_HEIGHT;
    },
  });
  Object.defineProperty(Element.prototype, 'scrollTop', {
    configurable: true,
    get() {
      return scrollTopValue;
    },
    set(v: number) {
      writes += 1;
      lastWrittenTop = v;
      // 写进去要沉淀：`onWheel` 的 `el.scrollTop <= 0` 闸门与 `nearBottom` 都读它，
      // 不沉淀会让「上滚脱离」这条路不可达（用例会退化成平凡成立）。
      scrollTopValue = v;
    },
  });
}

function resetCounters(): void {
  reads = 0;
  writes = 0;
  lastWrittenTop = null;
}

// ── 夹具：用**真投影**折叠真事件，与生产形状同源 ──

const fold = (conv: ConversationState, events: AgentEvent[]): ConversationState =>
  events.reduce(applyEvent, conv);

/** 流式中的会话：1 轮，`run_status` 停在 `running`（故意不给 `run/completed`）。 */
const STREAM_SEED: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: { agent_profile: 'main' }, seq: 1, run_id: 'r1', session_id: 'f3' },
  { type: EventType.USER_MESSAGE, data: { content: 'hi', step: 1 }, seq: 2, run_id: 'r1', step_id: 1, session_id: 'f3' },
  { type: EventType.MODEL_DELTA, data: { delta: '流式' }, seq: 3, run_id: 'r1', step_id: 1, session_id: 'f3' },
];

const streamSeed = (): ConversationState => fold(initConversation('f3'), STREAM_SEED);

/** 一帧内连续提交用的 N 个**互不相同**的投影状态（每次都是新的顶层对象 ⇒ effect 必重跑）。 */
function deltas(from: ConversationState, n: number, seqFrom = 100): ConversationState[] {
  const out: ConversationState[] = [];
  let cur = from;
  for (let i = 0; i < n; i++) {
    cur = fold(cur, [
      { type: EventType.MODEL_DELTA, data: { delta: `+${i}` }, seq: seqFrom + i, run_id: 'r1', step_id: 1, session_id: 'f3' },
    ]);
    out.push(cur);
  }
  return out;
}

/** 一个已收口的 run（`run_status === 'completed'`）。 */
const DONE_SEED: AgentEvent[] = [
  ...STREAM_SEED,
  { type: EventType.RUN_COMPLETED, data: {}, seq: 4, run_id: 'r1', session_id: 'f3' },
];

// ── 渲染夹具（照抄 Conversation.memo.test.tsx 的约定）──

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(conversation: ConversationState): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(<Conversation conversation={conversation} loadingHistory={false} density="balanced" />));
}

const scroller = (): HTMLElement => container!.querySelector('.conversation-scroll') as HTMLElement;

/** 等真实的下一帧（rAF 回调已全部执行完）。 */
const nextFrame = (): Promise<void> =>
  new Promise<void>((res) => {
    globalThis.requestAnimationFrame(() => res());
  });

/** 挂载 + 等挂载那一拍贴底落地，再把计数器归零。
 *
 *  改造后贴底是**延后一帧**的，所以「挂载后立刻读计数」看到的是 0 —— 后续每条断言
 *  都必须先让挂载那一拍走完，否则测到的是挂载与用例自身的竞态，而不是被测路径。 */
async function mountStream(seed: ConversationState): Promise<void> {
  render(seed);
  await nextFrame();
  resetCounters();
}

/** 真实滚轮上滚 —— 走 `setScrollNode` 的 wheel 监听（同步脱离跟随）。 */
function wheelUp(deltaY = -120): void {
  act(() => {
    scroller().dispatchEvent(new WheelEvent('wheel', { deltaY, bubbles: true }));
  });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  installScrollCounters();
  scrollTopValue = 0;
  resetCounters();
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  vi.restoreAllMocks();
});

// ── AC2 ──

describe('F3 — AC2 一个 rAF 帧内 `scrollHeight` 只读一次', () => {
  it('一帧内 3 次提交 ⇒ 读 1 次、写 1 次（改造前 3 / 3）', async () => {
    const seed = streamSeed();
    await mountStream(seed);
    const next = deltas(seed, 3);

    // 同一同步块内的 3 次提交：帧回调不可能插进来
    for (const s of next) render(s);
    await nextFrame();
    expect(reads).toBe(1);
    expect(writes).toBe(1);
  });

  it('一帧内 5 次提交 ⇒ 仍然是读 1 次（去重不是「第一次之后就不干了」）', async () => {
    const seed = streamSeed();
    await mountStream(seed);
    const next = deltas(seed, 5);

    for (const s of next) render(s);
    await nextFrame();
    expect(reads).toBe(1);
    expect(writes).toBe(1);
  });

  it('跨两帧各 2 次提交 ⇒ 每帧各读 1 次（收进单帧 ≠ 攒批到很晚）', async () => {
    const seed = streamSeed();
    await mountStream(seed);
    const [a, b, c, d] = deltas(seed, 4);

    render(a);
    render(b);
    await nextFrame();
    expect(reads).toBe(1);
    expect(writes).toBe(1);

    resetCounters();
    render(c);
    render(d);
    await nextFrame();
    expect(reads).toBe(1);
    expect(writes).toBe(1);
  });

  it('贴底仍是**瞬时**的：写进去的就是读到的 `scrollHeight`（不得引入 smooth/scrollIntoView）', async () => {
    const seed = streamSeed();
    await mountStream(seed);
    const next = deltas(seed, 2);

    for (const s of next) render(s);
    await nextFrame();
    expect(lastWrittenTop).toBe(SCROLL_HEIGHT);
  });

  it('前提自检：挂载那一拍确实贴了一次底（容器在、follow 为真）', async () => {
    render(streamSeed());
    resetCounters();
    await nextFrame();
    expect(reads).toBe(1);
    expect(writes).toBe(1);
    expect(lastWrittenTop).toBe(SCROLL_HEIGHT);
  });
});

// ── AC3 ──

describe('F3 — AC3 闸门为假时不得发生任何 `scrollTop` 写入', () => {
  it('`run_status !== running` ⇒ 0 次写入', async () => {
    const done = fold(initConversation('f3'), DONE_SEED);
    render(done);
    resetCounters();
    for (const s of deltas(done, 3, 200)) render(s);
    await nextFrame();
    expect(writes).toBe(0);
  });

  it('用户上滚脱离后（`following === false`）⇒ 0 次写入，且「↓ 最新」浮标出现', async () => {
    const seed = streamSeed();
    await mountStream(seed);
    // 挂载贴底已把 scrollTop 写成 SCROLL_HEIGHT ⇒ wheel 闸门 `el.scrollTop <= 0` 放行
    wheelUp();
    expect(container!.querySelector('.follow-pill')).not.toBeNull();

    resetCounters();
    for (const s of deltas(seed, 3)) render(s);
    await nextFrame();
    expect(writes).toBe(0);
  });
});

// ── AC4 ──

describe('F3 — AC4 卸载时已登记的帧被取消', () => {
  it('卸载后已登记的 rAF 被 `cancelAnimationFrame` 掉（挂起的贴底不得执行）', async () => {
    const seed = streamSeed();
    await mountStream(seed);
    const next = deltas(seed, 1);

    const rafSpy = vi.spyOn(globalThis, 'requestAnimationFrame');
    const cancelSpy = vi.spyOn(globalThis, 'cancelAnimationFrame');

    render(next[0]); // 登记一帧
    expect(rafSpy).toHaveBeenCalledTimes(1);
    const handle = rafSpy.mock.results[0].value as number;

    act(() => root!.unmount());
    container?.remove();
    container = null;
    root = null;

    expect(cancelSpy).toHaveBeenCalledWith(handle);
  });

  it('卸载后过一帧不产生任何 `scrollTop` 写入（行为守卫）', async () => {
    const seed = streamSeed();
    await mountStream(seed);
    const next = deltas(seed, 1);

    render(next[0]);
    act(() => root!.unmount());
    container?.remove();
    container = null;
    root = null;

    resetCounters();
    await nextFrame();
    expect(writes).toBe(0);
    expect(reads).toBe(0);
  });
});

// ── 行为守卫：本票不得破坏的既有路径 ──

describe('F3 — 反例守卫（本票不得把既有路径一起改掉）', () => {
  it('run 结束那一拍仍补一次贴底，且是**同步**的（不得被本票改成异步）', async () => {
    await mountStream(streamSeed());
    // running → completed：`wasActive && !runActive && wasFollowing` ⇒ 补底
    render(fold(initConversation('f3'), DONE_SEED));
    expect(writes).toBe(1);
    expect(lastWrittenTop).toBe(SCROLL_HEIGHT);
  });

  it('「↓ 最新」点击仍是**同步**瞬时贴底（点一下就得动，不能等一帧）', async () => {
    await mountStream(streamSeed());
    wheelUp();
    const pill = container!.querySelector('.follow-pill') as HTMLElement;
    expect(pill).not.toBeNull();

    resetCounters();
    act(() => pill.click());
    expect(writes).toBe(1);
    expect(lastWrittenTop).toBe(SCROLL_HEIGHT);
    expect(container!.querySelector('.follow-pill')).toBeNull();
  });

  it('真实滚轮上滚仍能脱离跟随（wheel 监听不得因为本票被绕过）', async () => {
    await mountStream(streamSeed());
    expect(container!.querySelector('.follow-pill')).toBeNull();

    wheelUp();
    expect(container!.querySelector('.follow-pill')).not.toBeNull();
  });
});
