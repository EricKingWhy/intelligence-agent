// @vitest-environment jsdom
/** #1429 尾项：轮次导航「当前所在轮」高亮（`.is-on`）的 DOM 落地。
 *
 *  判定口径是 `lib/turnNav.ts::currentTurnIndex`（阅读位置 = 滚动容器顶边，行偏移来自
 *  虚拟器；表驱动在 `lib/turnNav.test.ts`）。这里只锁渲染层三件事：
 *  ① 只有当前轮那一颗药丸带 `.is-on` / `aria-current="location"`；
 *  ② 滚动（容器 `scroll` 事件 + 虚拟器给出的新 `scrollOffset`）后高亮跟着走；
 *  ③ 置灰药丸（不可达轮次）永远不会同时是当前轮。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { initConversation } from '../lib/projection';
import type { ConversationState, Turn } from '../types';

/** 虚拟器替身：行窗口与滚动位置由用例直接改（真虚拟器在 jsdom 里没有布局）。 */
const vstate = vi.hoisted(() => ({
  offset: 0,
  rows: [] as { index: number; start: number; end: number; key: number; size: number; lane: number }[],
}));

vi.mock('@tanstack/react-virtual', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-virtual')>();
  // 稳定实例（与真 `useVirtualizer` 一致：实例跨渲染不变），否则每次渲染换 identity
  // 会让 Conversation 里的 effect 反复重挂。
  const instance = {
    getVirtualItems: () => vstate.rows,
    getTotalSize: () => 720,
    measureElement: () => {},
    scrollToIndex: () => {},
    get scrollOffset() {
      return vstate.offset;
    },
  };
  return { ...actual, useVirtualizer: () => instance };
});

import { Conversation } from './Conversation';

const T0 = '2026-09-18T00:00:00.000Z';

function turn(stepId: number, turnIndex: number): Turn {
  return {
    step_id: stepId,
    user_message: 'hi',
    model: { text: 'answer', status: 'done' },
    segments: [{ text: 'answer', status: 'done' }],
    tools: [],
    activities: [{ kind: 'model', index: 0 }],
    status: 'done',
    turn_index: turnIndex,
    user_message_seq: stepId,
    started_at: T0,
    completed_at: '2026-09-18T00:00:03.000Z',
  };
}

function rowsFor(count: number) {
  return Array.from({ length: count }, (_, i) => ({
    index: i,
    start: i * 240,
    end: i * 240 + 240,
    key: i,
    size: 240,
    lane: 0,
  }));
}

describe('#1429 轮次导航当前轮高亮', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
    vstate.rows = rowsFor(3);
    vstate.offset = 0;
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    delete (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
  });

  /** 挂载并等一拍：首次判定走微任务（effect 内不同步 setState）。 */
  const mount = (conversation: ConversationState) =>
    act(async () => {
      root.render(
        <Conversation conversation={conversation} loadingHistory={false} density="balanced" />,
      );
      await new Promise((r) => setTimeout(r, 32));
    });

  /** 容器滚动：真浏览器里由 scroll 事件触发；这里补一次宏任务等 rAF 回调落地。 */
  const scrollTo = (offset: number) =>
    act(async () => {
      vstate.offset = offset;
      const el = container.querySelector('.conversation-scroll');
      expect(el).not.toBeNull();
      el!.dispatchEvent(new Event('scroll'));
      await new Promise((r) => setTimeout(r, 32));
    });

  const pills = () => [...container.querySelectorAll<HTMLButtonElement>('.turn-nav-pill')];

  it('高亮只在当前轮那一颗药丸上，滚动后跟着阅读位置移动', async () => {
    await mount({
      ...initConversation('s-turn-nav'),
      turns: [turn(1, 1), turn(2, 2), turn(3, 3)],
    });
    expect(pills()).toHaveLength(3);
    expect(pills().map((p) => p.classList.contains('is-on'))).toEqual([true, false, false]);
    expect(pills()[0].getAttribute('aria-current')).toBe('location');
    expect(pills()[1].getAttribute('aria-current')).toBeNull();

    await scrollTo(300);
    expect(pills().map((p) => p.classList.contains('is-on'))).toEqual([false, true, false]);
    expect(pills()[1].getAttribute('aria-current')).toBe('location');

    // 滚动回顶部（上行方向同样生效）。
    await scrollTo(0);
    expect(pills().map((p) => p.classList.contains('is-on'))).toEqual([true, false, false]);
  });

  it('置灰药丸（压缩前缀 / 不可达轮次）永远不会同时是当前轮', async () => {
    vstate.rows = rowsFor(2);
    await mount({
      ...initConversation('s-turn-nav-off'),
      turns: [turn(3, 3), turn(4, 4)],
      compactions: [{ compacted_turn_count: 2 } as never],
    });
    const all = pills();
    expect(all).toHaveLength(4);
    expect(all.map((p) => p.classList.contains('is-off'))).toEqual([true, true, false, false]);
    expect(all.map((p) => p.classList.contains('is-on'))).toEqual([false, false, true, false]);
  });
});
