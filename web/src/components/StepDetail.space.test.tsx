// @vitest-environment jsdom
/** #1419 AC2：Space 的「快按 = 保持打开 / 按住 = 松手关闭」判据。
 *
 *  为什么要有这一条组件级测试：判据从 `spaceReleaseCloses(heldMs)`（两事件时间戳
 *  相减，纯函数、易测）改成了 `spaceHoldTimer`（阈值到点时键还按着吗）。纯函数
 *  连同它的边界用例一起被删掉后，这条语义只剩 e2e 一条覆盖——而它历史上**正是
 *  在并行负载下翻过车**（`keyboard.press('Space')` 被量成长按，peek 刚开就关）。
 *  这里用假定时器直接驱动"阈值前松手 / 阈值后松手"两个边界，几毫秒内跑完。
 *
 *  宿主/驱动照抄 `ApprovalCard.approvePolicy.test.tsx`（jsdom + createRoot + act）。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { applyEvent, initConversation } from '../lib/projection';
import { PEEK_TAP_MS } from '../lib/inspectorPanel';
import { StepDetail, type InspectorFocus, type InspectorPanelState } from './StepDetail';

const noop = () => {};

function panel(over: Partial<InspectorPanelState> = {}): InspectorPanelState {
  return { pinned: false, expanded: false, width: 320, peekOpen: false, ...over };
}

function conversation(): ConversationState {
  let s = initConversation('s1419');
  const events: AgentEvent[] = [
    { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 's1419', event_id: 'e1' },
    { type: EventType.TOOL_CALL, data: { tool_call_id: 'tc1', name: 'bash', args: { command: 'ls' } }, seq: 2, run_id: 'r1', step_id: 1, session_id: 's1419', event_id: 'e2' },
    { type: EventType.RUN_COMPLETED, data: {}, seq: 3, run_id: 'r1', session_id: 's1419', event_id: 'e3' },
  ];
  for (const e of events) s = applyEvent(s, e);
  return s;
}

let container: HTMLDivElement;
let root: Root;
const actions: InspectorPanelState extends never ? never : Array<Record<string, unknown>> = [];

function mount(over: Partial<InspectorPanelState> = {}) {
  const conv = conversation();
  const focus: InspectorFocus = { kind: 'event', event: conv.events[1] };
  const el: ReactElement = (
    <StepDetail
      conversation={conv}
      streaming={false}
      focus={focus}
      onFocusRun={noop}
      onFocusTool={noop}
      onFocusEvent={noop}
      panel={panel(over)}
      onPanelAction={(a) => actions.push(a as unknown as Record<string, unknown>)}
    />
  );
  act(() => {
    root.render(el);
  });
  const aside = container.querySelector('.step-detail');
  if (!(aside instanceof HTMLElement)) throw new Error('没有面板根节点');
  return aside;
}

const press = (el: HTMLElement, type: 'keydown' | 'keyup') => {
  act(() => {
    el.dispatchEvent(new KeyboardEvent(type, { key: ' ', code: 'Space', bubbles: true }));
  });
};

const actionTypes = () => actions.map((a) => a.type);

beforeEach(() => {
  vi.useFakeTimers();
  /* jsdom 没有 scrollIntoView，选中项滚动的 effect 会直接抛错（组件本身没错）。 */
  Element.prototype.scrollIntoView = () => {};
  actions.length = 0;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.useRealTimers();
});

describe('#1419 AC2 Space：快按保持打开，按住松手关闭', () => {
  it('按下即打开预览（peekOpen=false 时 Space = 开 peek）', () => {
    const el = mount();
    press(el, 'keydown');
    expect(actionTypes()).toEqual(['open-peek']);
    press(el, 'keyup');
  });

  it('阈值前松手 = 快按：不关预览', () => {
    const el = mount();
    press(el, 'keydown');
    act(() => {
      vi.advanceTimersByTime(PEEK_TAP_MS - 50);
    });
    press(el, 'keyup');
    expect(actionTypes()).toEqual(['open-peek']);
  });

  it('阈值后松手 = 按住：松手关预览', () => {
    const el = mount({ peekOpen: true });
    press(el, 'keydown');
    act(() => {
      vi.advanceTimersByTime(PEEK_TAP_MS + 50);
    });
    press(el, 'keyup');
    // peekOpen 已是 true ⇒ keydown 不发 open-peek，只有松手那一下关。
    expect(actionTypes()).toEqual(['close-peek']);
  });

  it('重复事件不重置计时（长按的 e.repeat 序列仍算一次按住）', () => {
    const el = mount({ peekOpen: true });
    press(el, 'keydown');
    act(() => {
      vi.advanceTimersByTime(PEEK_TAP_MS - 10);
    });
    /* 阈值前一瞬来一次 repeat：若它重置了计时，阈值到点就来不及置 held，
       松手会被当成快按（不关）。 */
    act(() => {
      el.dispatchEvent(new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, repeat: true }));
    });
    act(() => {
      vi.advanceTimersByTime(20);
    });
    press(el, 'keyup');
    expect(actionTypes()).toEqual(['close-peek']);
  });
});
