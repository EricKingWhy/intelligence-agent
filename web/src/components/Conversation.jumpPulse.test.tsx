// @vitest-environment jsdom
/** #459：Inspector 待审批行 → **内联**审批卡的反向联动（jumpRequest key
 *  `approval:<id>`）组件级覆盖。
 *
 *  为什么落点只剩内联卡：#421 起首个**非失效**待决审批进常驻模态（模态打开时
 *  应用 inert），而模态候选身上不挂 `data-approval-key`（`Conversation.tsx:438-441`：
 *  portal 在 conversation 根之外，对模态候选的跳转是设计内 no-op）⇒ 反向联动的
 *  可达对象 = 失效孤儿（stale / 404-gone）与多卡并存的第 2+ 张。原唯一覆盖是
 *  e2e（`3a579242` #184 引入），#441 改写移除后此路径零自动化覆盖
 *  （`grep -rn "stream-jump-pulse" web/src web/e2e` 只剩实现与 CSS）。
 *
 *  夹具：ap-1 判失效（走 `goneApprovalIds`，与 App 侧 404-gone 同一入口）+
 *  ap-2 live ⇒ `firstLiveIdx=1` ⇒ ap-1 内联渲染（带 `data-approval-key`）、
 *  ap-2 进模态。宿主照抄 Conversation.approvalEcho.test.tsx：假 useVirtualizer +
 *  真投影 + createRoot。
 *
 *  jsdom 不实现 `scrollIntoView`（打桩）；900ms 移除用捕获的 timeout 回调手动
 *  触发——不引假计时器，避免把 Radix 模态自用的定时器一起冻住。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@tanstack/react-virtual', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-virtual')>();
  return {
    ...actual,
    useVirtualizer: vi.fn(() => ({
      getVirtualItems: () => [],
      getTotalSize: () => 0,
      measureElement: () => {},
      scrollToIndex: () => {},
    })),
  };
});

import { applyEvent, initConversation } from '../lib/projection';
import type { TraceDensity } from '../lib/density';
import { EventType, type AgentEvent, type EventTypeValue } from '../types';
import type { ConversationState } from '../types';
import { Conversation } from './Conversation';

// ── 投影夹具（事件形状与 Conversation.approvalEcho.test.tsx 同源） ──

let seq = 0;
function ev(sid: string, type: EventTypeValue, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { session_id: sid, time: '2026-09-30T00:00:00Z', type, seq, run_id: 'r', step_id: 1, data };
}

function approvalData(id: string): Record<string, unknown> {
  return {
    approval_id: id,
    tool_name: 'bash',
    tool_call_id: `tc-${id}`,
    action_type: 'danger',
    title: 't',
    description: 'd',
    arguments_preview: {},
    permission: 'p',
    policy: 'pol',
    reason: '危险操作',
    allowed_decisions: ['approve_once', 'deny'],
  };
}

/** 一条有 turn 的会话 + 两张挂起审批：ap-1、ap-2（顺序即投影顺序）。 */
function twoPendingState(sid: string): ConversationState {
  let s = applyEvent(initConversation(sid), ev(sid, EventType.USER_MESSAGE, { content: '跑个命令', step: 1 }));
  s = applyEvent(s, ev(sid, EventType.RUN_STARTED, { turn_index: 1 }));
  s = applyEvent(s, ev(sid, EventType.TOOL_APPROVAL_REQUESTED, approvalData('ap-1')));
  return applyEvent(s, ev(sid, EventType.TOOL_APPROVAL_REQUESTED, approvalData('ap-2')));
}

// ── 渲染夹具 ──

let container: HTMLDivElement | null = null;
let root: Root | null = null;
const scrollIntoViewSpy = vi.fn();
const originalScrollIntoView = Element.prototype.scrollIntoView;

function render(ui: ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

/** #459 夹具的全部 props：ap-1 失效（gone）⇒ 内联；ap-2 live ⇒ 模态。 */
function host(jumpRequest?: { key: string; nonce: number }): ReactElement {
  return (
    <Conversation
      conversation={twoPendingState('sA')}
      loadingHistory={false}
      density={'balanced' as TraceDensity}
      goneApprovalIds={new Set(['ap-1'])}
      jumpRequest={jumpRequest}
    />
  );
}

const inlineCard = () => container!.querySelector<HTMLElement>('[data-approval-key="ap-1"]')!;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  Element.prototype.scrollIntoView = scrollIntoViewSpy;
  scrollIntoViewSpy.mockClear();
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  if (originalScrollIntoView) Element.prototype.scrollIntoView = originalScrollIntoView;
  else Reflect.deleteProperty(Element.prototype, 'scrollIntoView');
});

describe('#459 — approval: jumpRequest → 内联卡脉冲', () => {
  it('ap-1 内联渲染（失效孤儿），ap-2 进模态——夹具确在目标路径上', () => {
    render(host());
    const inline = inlineCard();
    expect(inline).not.toBeNull();
    // 模态候选不挂 data-approval-key（Conversation.tsx:438-441 的设计约束）
    expect(container!.querySelectorAll('[data-approval-key]')).toHaveLength(1);
    expect(container!.querySelector('.stream-jump-pulse')).toBeNull(); // 无请求 ⇒ 无脉冲
  });

  it('jump approval:ap-1 → 卡片加 stream-jump-pulse + scrollIntoView，900ms 回调移除', () => {
    const timeoutSpy = vi.spyOn(window, 'setTimeout');
    try {
      render(host({ key: 'approval:ap-1', nonce: 1 }));

      const el = inlineCard();
      expect(el.className).toContain('stream-jump-pulse');
      expect(scrollIntoViewSpy).toHaveBeenCalledWith({ behavior: 'smooth', block: 'center' });

      const pulseCall = timeoutSpy.mock.calls
        .filter((call): call is [() => void, number] => typeof call[0] === 'function' && call[1] === 900)
        .pop();
      expect(pulseCall, '应有 900ms 的 pulse 移除定时器').toBeTruthy();
      act(() => pulseCall![0]());
      expect(inlineCard().className).not.toContain('stream-jump-pulse');
    } finally {
      timeoutSpy.mockRestore(); // 失败路径也不把 spy 泄漏给同文件后续用例
    }
  });

  it('同 nonce 重渲染不重触发（processedJumpNonce 守卫）；新 nonce 再触发（装置非 vacuous）', () => {
    render(host({ key: 'approval:ap-1', nonce: 1 }));
    expect(inlineCard().className).toContain('stream-jump-pulse');

    // 模拟 900ms 已过（摘掉 class），同 nonce、新对象再渲染：effect 重跑但守卫拦下
    act(() => inlineCard().classList.remove('stream-jump-pulse'));
    act(() => {
      root!.render(host({ key: 'approval:ap-1', nonce: 1 }));
    });
    expect(inlineCard().className).not.toContain('stream-jump-pulse');

    // 正对照：新 nonce（同目标）必须重新脉冲——否则「不重触发」可能只是装置失灵
    act(() => {
      root!.render(host({ key: 'approval:ap-1', nonce: 2 }));
    });
    expect(inlineCard().className).toContain('stream-jump-pulse');
  });

  it('未知 approval key：无脉冲（负向对照——查询不是「匹配一切」）', () => {
    render(host({ key: 'approval:ap-404', nonce: 1 }));
    expect(container!.querySelector('.stream-jump-pulse')).toBeNull();
  });
});
