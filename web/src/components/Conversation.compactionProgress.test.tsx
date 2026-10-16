// @vitest-environment jsdom
/** #1197 项 5A 组件面：压缩进行中时**必须**显示「正在压缩上下文…」，不许静默。
 *
 *  断言口径取自效果图 02（`docs/ux-mockups/2026-10-16-context-compact/mockup.html:699-703`）：
 *  `role="status" aria-live="polite"` 的条 + 16px spinner + 正文 + `<small>运行会自动继续</small>`；
 *  `COMPACTION_END` 到达后整条消失（自动恢复常态）。
 *
 *  虚拟化 mock 成全量渲染（本仓既有模式，见 Conversation.memoryRecall.test.tsx）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('@tanstack/react-virtual', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-virtual')>();
  return {
    ...actual,
    useVirtualizer: vi.fn((options: { count: number }) => ({
      getVirtualItems: () =>
        Array.from({ length: options.count }, (_, index) => ({ index, key: index })),
      getTotalSize: () => options.count * 240,
      measureElement: () => {},
      scrollToIndex: () => {},
    })),
  };
});

import { applyEvent, initConversation } from '../lib/projection';
import type { AgentEvent, ConversationState } from '../types';
import { Conversation } from './Conversation';

function ev(type: string, data: Record<string, unknown>, seq: number): AgentEvent {
  return { session_id: 's1', time: '2026-10-16T00:00:00Z', type, seq, run_id: 'r1', step_id: null, data };
}

/** 一个「压缩进行中」的会话：START 已到、END 未到。 */
function compressingState(): ConversationState {
  let s = initConversation('s1');
  s = applyEvent(s, ev('user/message', { content: '继续' }, 1));
  s = applyEvent(s, ev('run/started', { turn_index: 1 }, 2));
  s = applyEvent(s, ev('compaction/start', { bracket_id: 'b1' }, 3));
  return s;
}

function endedState(): ConversationState {
  return applyEvent(compressingState(), ev('compaction/end', { bracket_id: 'b1' }, 4));
}


let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(ui: ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

function bar(): HTMLElement | null {
  return container!.querySelector<HTMLElement>('.compaction-progress');
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
});

describe('Conversation — 压缩进行中（#1197 项 5A）', () => {
  it('START 已到、END 未到 ⇒ 显示「正在压缩上下文…」，不许静默', () => {
    render(<Conversation conversation={compressingState()} loadingHistory={false} density="balanced" />);
    const el = bar();
    expect(el).not.toBeNull();
    expect(el!.textContent).toContain('正在压缩上下文…');
    // 效果图 02：副文案说明"不用等"，且读屏可播报（不许静默）。
    expect(el!.textContent).toContain('运行会自动继续');
    expect(el!.getAttribute('role')).toBe('status');
    expect(el!.getAttribute('aria-live')).toBe('polite');
  });

  it('END 到达 ⇒ 整条自动消失，恢复常态', () => {
    render(<Conversation conversation={endedState()} loadingHistory={false} density="balanced" />);
    expect(bar()).toBeNull();
    expect(container!.textContent).not.toContain('正在压缩上下文');
  });

  it('阴性对照：没有压缩事件的会话不渲染该条', () => {
    let s = initConversation('s1');
    s = applyEvent(s, ev('user/message', { content: '你好' }, 1));
    render(<Conversation conversation={s} loadingHistory={false} density="balanced" />);
    expect(bar()).toBeNull();
  });


});
