// @vitest-environment jsdom
/** N2（#271）AC7：`events` 追加后 Timeline 的 run 分组必须更新。
 *
 *  为什么单独一个文件、且要 jsdom：这条判的是「**同一实例**在父级提交后有没有重算
 *  派生值」，而 `useMemo` 的 bail-out 只在客户端渲染器的 reconciliation 里发生。
 *  本仓 `StepDetail.test.tsx` 是 SSR（`renderToString`）契约测试——每次调用都是全新
 *  一次渲染，`useMemo` 必然重算，陈旧 memo **在 node 车道里不可观测**。沿用 F1（#270）
 *  的 `Conversation.render.test.tsx` 同款做法：文件级 `@vitest-environment`，不动既有
 *  文件的 SSR 车道。
 *
 *  缺陷形态（改造前）：`useMemo(..., [conversation.events])`。`events` 是 P0-1 的
 *  append-only 共享数组，`applyEvent` 返回**新 state 对象但同一个 events 引用**——
 *  于是父级提交时依赖比较恒等，派生的 run 分组停在首帧：新 run 的组头永远不出现，
 *  老组的「N 事件」计数也永远不涨（Inspector 显示陈旧内容 = 正确性缺陷）。
 *
 *  夹具刻意让「新 state 对象 + 同一 events 引用」这一对同时成立，并用 `===` 断言
 *  钉住它——否则本用例会退化成「测了一个不存在的场景」。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { applyEvent, initConversation } from '../lib/projection';
import { TimelineTab } from './StepDetail';

const noop = () => {};

/** run 1：4 个事件，终态 completed。 */
const RUN1: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'n2' },
  { type: EventType.USER_MESSAGE, data: { content: 'hi', step: 1 }, seq: 2, run_id: 'r1', step_id: 1, session_id: 'n2' },
  { type: EventType.MODEL_COMPLETED, data: { content: 'ok', step: 1 }, seq: 3, run_id: 'r1', step_id: 1, session_id: 'n2' },
  { type: EventType.RUN_COMPLETED, data: {}, seq: 4, run_id: 'r1', session_id: 'n2' },
];

/** run 2：追加 2 个事件（一个是流式 delta）。 */
const RUN2: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: {}, seq: 5, run_id: 'r2', session_id: 'n2' },
  { type: EventType.MODEL_DELTA, data: { delta: 'x' }, seq: 6, run_id: 'r2', step_id: 2, session_id: 'n2' },
];

function fold(conv: ConversationState, events: AgentEvent[]): ConversationState {
  return events.reduce(applyEvent, conv);
}

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  // 与 Conversation.render.test.tsx 同款：不置此标记时 React 会对每次 act() 打
  // 「environment is not configured to support act(...)」告警（AC9 要求零新增告警）。
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

/** 同一个 root 再渲染一次——`conversation` 是新对象、`events` 是同一个数组。 */
const paint = (conversation: ConversationState) =>
  act(() => {
    root.render(createElement(TimelineTab, { conversation, onFocusEvent: noop }));
  });

const headers = () =>
  Array.from(container.querySelectorAll('.tl-run-header')).map((n) => n.textContent ?? '');
const rowCount = () => container.querySelectorAll('.timeline-row').length;

describe('N2（#271）Timeline run 分组必须跟随 events 追加更新', () => {
  it('AC7 追加 run 2 后：组头从 1 个变 2 个、行数从 4 变 6（改造前均为陈旧值）', () => {
    const first = fold(initConversation('n2'), RUN1);
    paint(first);
    expect(headers()).toHaveLength(1);
    expect(headers()[0]).toContain('Run 1');
    expect(rowCount()).toBe(4);

    // 追加 → 新 state 对象，但 events 是**同一个**引用（append-only 契约）。
    const second = fold(first, RUN2);
    expect(second).not.toBe(first);
    expect(second.events).toBe(first.events);
    expect(first.events).toHaveLength(6); // 原地 push：旧 state 的视图也一起涨了

    paint(second);

    // 改造前这里回落到 1 / 4：memo 依赖 `events`（引用恒等）⇒ 派生值停在首帧。
    expect(headers()).toHaveLength(2);
    expect(headers()[0]).toContain('Run 1');
    expect(headers()[1]).toContain('Run 2');
    expect(rowCount()).toBe(6);
  });

  it('AC7 已存在组的计数与状态也刷新（不是只追加新组头就完事）', () => {
    const first = fold(initConversation('n2'), RUN1);
    paint(first);
    expect(headers()[0]).toContain('4 事件');
    expect(headers()[0]).toContain('已完成');

    // 同 run 追加一个终态事件：计数 4→5，且状态由 completed 保持（组内已有终态）。
    const second = fold(first, [
      { type: EventType.MODEL_DELTA, data: { delta: 'y' }, seq: 7, run_id: 'r1', step_id: 1, session_id: 'n2' },
    ]);
    expect(second.events).toBe(first.events);
    paint(second);

    expect(headers()).toHaveLength(1);
    expect(headers()[0]).toContain('5 事件');
    expect(rowCount()).toBe(5);
  });
});
