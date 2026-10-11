// @vitest-environment jsdom
/** #1017 消融实验（票面 4 条）+ 验收 #1/#2/#3 的组件级呈现测试。
 *
 * 会话夹具镜像真机症状会话（issue #1017）：5 轮、每轮 1 条 memory/recalled
 * （seq 单调、run_id 各异）。虚拟化器 mock 成全量渲染（本仓既有模式，
 * 见 Conversation.jumpPulse.test.tsx）——虚拟化测高本身不受轮内内容影响的
 * 交互验证归 e2e（真实浏览器 measureElement）。
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
import type { AgentEvent, ConversationState, EventTypeValue } from '../types';
import { Conversation } from './Conversation';

// ── 事件流夹具 ──

function ev(sid: string, type: EventTypeValue | string, data: Record<string, unknown>, stepId: number | null, runId: string, seq: number): AgentEvent {
  return { session_id: sid, time: '2026-10-12T00:00:00Z', type, seq, run_id: runId, step_id: stepId, data };
}

/** 5 轮、每轮恰好 1 条 memory/recalled（镜像 issue 真机会话的事件形态）。
 *  seq 每个 state 从 1 独立计数（第 i 轮的 recalled seq = (i-1)*4+3）。 */
function multiTurnState(sid: string, turns: number, withRecalls: boolean): ConversationState {
  let seq = 0;
  let s = initConversation(sid);
  for (let i = 1; i <= turns; i += 1) {
    const runId = `run-${i}`;
    s = applyEvent(s, ev(sid, 'user/message', { content: `问题 ${i}` }, i, runId, ++seq));
    s = applyEvent(s, ev(sid, 'run/started', { turn_index: i }, null, runId, ++seq));
    if (withRecalls) {
      s = applyEvent(s, ev(sid, 'memory/recalled', { memories: [] }, null, runId, ++seq));
    }
    s = applyEvent(s, ev(sid, 'model/completed', { content: `回答 ${i}` }, i, runId, ++seq));
  }
  return s;
}

// ── 渲染夹具 ──

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

function recallCards(): HTMLElement[] {
  return [...container!.querySelectorAll<HTMLElement>('.memory-v2-activity-card')]
    .filter((el) => el.textContent?.includes('记忆召回说明'));
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  act(() => root?.unmount());
  root = null;
  container?.remove();
  container = null;
  vi.unstubAllGlobals();
});

describe('Conversation — memory/recalled 按轮归位（#1017 消融）', () => {
  it('消融 #3：5 轮各 1 条 recalled → 每轮至多一张卡、且在轮次内，尾部无游离卡', () => {
    render(<Conversation conversation={multiTurnState('s-1', 5, true)} loadingHistory={false} density="balanced" />);

    const cards = recallCards();
    expect(cards).toHaveLength(5);
    for (const card of cards) {
      expect(card.closest('.turn')).not.toBeNull(); // 在某一轮内
    }
    // 每轮恰好一张
    for (const turn of container!.querySelectorAll<HTMLElement>('.turn')) {
      expect(turn.querySelectorAll('.memory-v2-activity-card')).toHaveLength(1);
    }
  });

  it('消融 #2（阴性对照）：新会话单轮 → 恰好 1 张卡（历史累积才是 N 卡根因）', () => {
    render(<Conversation conversation={multiTurnState('s-2', 1, true)} loadingHistory={false} density="balanced" />);
    expect(recallCards()).toHaveLength(1);
  });

  it('消融 #4：无 memory/recalled 的会话 → 任何位置都无召回卡', () => {
    render(<Conversation conversation={multiTurnState('s-3', 2, false)} loadingHistory={false} density="balanced" />);
    expect(recallCards()).toHaveLength(0);
    expect(container!.querySelectorAll('.memory-v2-activity-card')).toHaveLength(0);
  });

  it('验收 #3：展开后按事件 seq/run_id 对账 /memory-recalls，只渲染对应记录', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([
      {
        run_id: 'run-1', seq: 3, time: '2026-10-12T00:00:00Z',
        memories: [{ memory_id: 'm-old', kind: 'semantic', scope: 'user_global', source_type: 'explicit_command', version: 2, source_session_id: 'src-s', source_event_ids: ['e1'], ranking: { score: 0.9 } }],
      },
      {
        run_id: 'run-3', seq: 11, time: '2026-10-12T00:00:00Z',
        memories: [{ memory_id: 'm-new', kind: 'semantic', scope: 'user_global', source_type: 'explicit_command', version: 3, source_session_id: 'src-s', source_event_ids: ['e3'], ranking: { score: 0.8 } }],
      },
    ]), { status: 200, headers: { 'content-type': 'application/json' } }));
    vi.stubGlobal('fetch', fetchMock);

    render(<Conversation conversation={multiTurnState('s-4', 5, true)} loadingHistory={false} density="balanced" />);

    // 第 3 轮的卡：展开后只出现 seq=9/run-3 的记录（对账成功），不串其他轮的
    const turns = container!.querySelectorAll<HTMLElement>('.turn');
    const thirdTurnCard = turns[2].querySelector<HTMLButtonElement>('.memory-v2-activity-toggle')!;
    await act(async () => { thirdTurnCard.click(); await Promise.resolve(); });

    expect(String(fetchMock.mock.calls[0][0])).toBe('/api/sessions/s-4/memory-recalls');
    // 渲染面是脱敏白名单（不含 memory_id/content）——用 source_event_ids 作对账标识
    expect(container!.textContent).toContain('e3'); // 第 3 轮 recalled（run-3/seq=11）的记录
    expect(container!.textContent).not.toContain('e1'); // 其他轮（run-1/seq=3）的记录不串卡
  });

  it('尾部块不再渲染召回卡；memory/updated 卡保持原位（#301 行为不回归）', () => {
    let s = multiTurnState('s-5', 2, true);
    s = applyEvent(s, ev('s-5', 'memory/updated', { count: 1, memory_ids: ['m-1'] }, null, 'run-2', 100));
    render(<Conversation conversation={s} loadingHistory={false} density="balanced" />);

    const updatedCard = [...container!.querySelectorAll<HTMLElement>('.memory-v2-activity-card')]
      .find((el) => el.textContent?.includes('已更新 1 条记忆'));
    expect(updatedCard).toBeDefined();
    expect(updatedCard!.closest('.turn')).toBeNull(); // updated 卡留在尾部块
    // 所有召回卡都在轮内
    for (const card of recallCards()) {
      expect(card.closest('.turn')).not.toBeNull();
    }
  });
});
