// @vitest-environment jsdom
/** #1106：Inspector 头标徽标按**当前聚焦 run** 取值（derive 层之上的 wiring）。
 *
 *  runState.test.ts 已锁 derive 函数的聚焦口径；本文件锁 StepDetail 的接线——
 *  `focus`（event/tool 的信封 run_id）→ `focusedRunId` → 头标徽标。接线断了
 *  （比如忘了把 focus 传进 derive），derive 测试全绿而界面依旧取末值。
 *
 *  宿主/驱动照抄 ApprovalCard.approvePolicy.test.tsx（jsdom + createRoot + act）。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { StepDetail, type InspectorFocus, type InspectorPanelAction, type InspectorPanelState } from './StepDetail';
import { initConversation, applyEvent } from '../lib/projection';
import { EventType, type AgentEvent, type ConversationState } from '../types';

const noop = () => {};
const PANEL: InspectorPanelState = { pinned: false, expanded: false, width: 360, peekOpen: false };

/** 带**信封 run_id** 的事件（头标聚焦归属按信封 run_id 对账）。 */
const evRun = (type: string, data: Record<string, unknown>, run_id: string, step_id: number | null = 1): AgentEvent =>
  ({ type, data, seq: null, run_id, step_id });

function buildMultiRunConversation(): { state: ConversationState; runAStarted: AgentEvent; runBStarted: AgentEvent } {
  const runAStarted = evRun(EventType.RUN_STARTED, { agent_profile: 'research_review', reasoning_effort: 'minimal' }, 'run-a');
  const runBStarted = evRun(EventType.RUN_STARTED, { agent_profile: 'coding', reasoning_effort: 'deep' }, 'run-b', 2);
  let s = initConversation('s1');
  s = applyEvent(s, runAStarted);
  s = applyEvent(s, evRun(EventType.RUN_COMPLETED, {}, 'run-a'));
  s = applyEvent(s, runBStarted);
  s = applyEvent(s, evRun(EventType.RUN_COMPLETED, {}, 'run-b', 2));
  return { state: s, runAStarted, runBStarted };
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

const query = <T extends Element>(sel: string) => document.body.querySelector<T>(sel);

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom 不实现 scrollIntoView（选中项滚动定位用）——空实现即可，与本测试无关。
  Element.prototype.scrollIntoView = Element.prototype.scrollIntoView ?? (() => {});
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
});

describe('StepDetail 头标徽标 — #1106 聚焦 run 接线', () => {
  it('聚焦 run A 的事件 → 头标显示 A 的档位；聚焦 run B → B 的', () => {
    const { state, runAStarted, runBStarted } = buildMultiRunConversation();
    const focusA: InspectorFocus = { kind: 'event', event: runAStarted };
    render(
      <StepDetail
        conversation={state}
        streaming={false}
        focus={focusA}
        onFocusRun={noop}
        onFocusTool={noop}
        onFocusEvent={noop}
        onPanelAction={noop as (action: InspectorPanelAction) => void}
        panel={PANEL}
      />,
    );
    expect(query('.detail-effort-badge')?.textContent).toContain('minimal');
    expect(query('.detail-profile-badge')?.textContent).toContain('research_review');

    act(() => root!.render(
      <StepDetail
        conversation={state}
        streaming={false}
        focus={{ kind: 'event', event: runBStarted }}
        onFocusRun={noop}
        onFocusTool={noop}
        onFocusEvent={noop}
        onPanelAction={noop as (action: InspectorPanelAction) => void}
        panel={PANEL}
      />,
    ));
    expect(query('.detail-effort-badge')?.textContent).toContain('deep');
    expect(query('.detail-profile-badge')?.textContent).toContain('coding');
  });

  it('无聚焦 run（run 级视图）→ 回退「取最后值」（回归）', () => {
    const { state } = buildMultiRunConversation();
    render(
      <StepDetail
        conversation={state}
        streaming={false}
        focus={{ kind: 'run' }}
        onFocusRun={noop}
        onFocusTool={noop}
        onFocusEvent={noop}
        onPanelAction={noop as (action: InspectorPanelAction) => void}
        panel={PANEL}
      />,
    );
    expect(query('.detail-effort-badge')?.textContent).toContain('deep');
    expect(query('.detail-profile-badge')?.textContent).toContain('coding');
  });

  it('聚焦工具（tool/call 事件信封 run_id）→ 显示该 run 的档位', () => {
    // tool/call 挂在 run-a；聚焦该工具应显示 run-a 的档位，而不是末次 run-b 的。
    let s = initConversation('s2');
    s = applyEvent(s, evRun(EventType.RUN_STARTED, { agent_profile: 'research_review', reasoning_effort: 'minimal' }, 'run-a'));
    s = applyEvent(s, evRun(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }, 'run-a'));
    s = applyEvent(s, evRun(EventType.TOOL_RESULT, { tool_call_id: 't1', content: JSON.stringify({ ok: true, message: '', data: null }) }, 'run-a'));
    s = applyEvent(s, evRun(EventType.RUN_COMPLETED, {}, 'run-a'));
    s = applyEvent(s, evRun(EventType.RUN_STARTED, { agent_profile: 'coding', reasoning_effort: 'deep' }, 'run-b', 2));

    const tool = s.turns.flatMap((t) => t.tools).find((t) => t.tool_call_id === 't1');
    expect(tool).toBeDefined();
    render(
      <StepDetail
        conversation={s}
        streaming={false}
        focus={{ kind: 'tool', tool: tool! }}
        onFocusRun={noop}
        onFocusTool={noop}
        onFocusEvent={noop}
        onPanelAction={noop as (action: InspectorPanelAction) => void}
        panel={PANEL}
      />,
    );
    expect(query('.detail-effort-badge')?.textContent).toContain('minimal');
    expect(query('.detail-profile-badge')?.textContent).toContain('research_review');
  });

  it('单 run 会话：聚焦与不聚焦同值（回归）', () => {
    let s = initConversation('s3');
    s = applyEvent(s, evRun(EventType.RUN_STARTED, { agent_profile: 'main', reasoning_effort: 'standard' }, 'run-1'));
    const started = s.events.find((e) => e.type === EventType.RUN_STARTED)!;
    render(
      <StepDetail
        conversation={s}
        streaming={false}
        focus={{ kind: 'event', event: started }}
        onFocusRun={noop}
        onFocusTool={noop}
        onFocusEvent={noop}
        onPanelAction={noop as (action: InspectorPanelAction) => void}
        panel={PANEL}
      />,
    );
    expect(query('.detail-effort-badge')?.textContent).toContain('standard');
    expect(query('.detail-profile-badge')?.textContent).toContain('main');
  });
});
