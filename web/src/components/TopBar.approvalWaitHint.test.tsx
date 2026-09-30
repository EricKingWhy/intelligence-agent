// @vitest-environment jsdom
/** #443：顶栏「等待审批」文案的判据——只认**未失效**的审批。
 *
 *  投影在 run 终结时把仍挂起的审批标 `stale`（`projection.ts::markPendingApprovalsStale`
 *  只置位、不移出数组），`pending_approvals.length > 0` ≠ 有事在等。顶栏若按 length
 *  判「在等审批」，stale 审批 + 新 run 运行中时会把等待的主语说错——提示换成
 *  「正在等待你的审批决定」，而真正在等的是模型。正统判据是投影自己的
 *  `awaitingApproval`（`App.tsx` 已在用）。
 *
 *  提示文案的真相与阈值在 `runState.waitingHintText`（≥30s 才出文案），静态渲染
 *  （idleSec=0）永远看不到 ⇒ 需要 jsdom + 假计时器真实走 effect。夹具用**真投影**
 *  （applyEvent 造 stale / live 两种审批状态），渲染宿主照抄 ApprovalModal.test.tsx。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { applyEvent, initConversation } from '../lib/projection';
import type { TraceDensity } from '../lib/density';
import type { Theme } from '../lib/theme';
import { EventType, type AgentEvent, type ConversationState, type EventTypeValue } from '../types';
import { TopBar } from './TopBar';

// ── 投影夹具（事件形状与 Conversation.approvalEcho.test.tsx 同源） ──

let seq = 0;
function ev(sid: string, type: EventTypeValue, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { session_id: sid, time: '2026-09-30T00:00:00Z', type, seq, run_id: 'r', step_id: 1, data };
}

const APPROVAL_DATA = {
  approval_id: 'ap-1',
  tool_name: 'bash',
  tool_call_id: 'tc-1',
  action_type: 'danger',
  title: 't',
  description: 'd',
  arguments_preview: {},
  permission: 'p',
  policy: 'pol',
  reason: '危险操作',
  allowed_decisions: ['approve_once', 'deny'],
};

/** 审批挂着 → run 终结（投影判 stale，数组不清空）→ **新 run 运行中**。 */
function staleApprovalState(sid: string): ConversationState {
  let s = applyEvent(initConversation(sid), ev(sid, EventType.USER_MESSAGE, { content: '跑个命令', step: 1 }));
  s = applyEvent(s, ev(sid, EventType.RUN_STARTED, { turn_index: 1 }));
  s = applyEvent(s, ev(sid, EventType.TOOL_APPROVAL_REQUESTED, APPROVAL_DATA));
  s = applyEvent(s, ev(sid, EventType.RUN_COMPLETED, {}));
  return applyEvent(s, ev(sid, EventType.RUN_STARTED, { turn_index: 2 }));
}

/** 审批挂着且 run 正等它决策（未终结 → 未 stale）。 */
function liveApprovalState(sid: string): ConversationState {
  let s = applyEvent(initConversation(sid), ev(sid, EventType.USER_MESSAGE, { content: '跑个命令', step: 1 }));
  s = applyEvent(s, ev(sid, EventType.RUN_STARTED, { turn_index: 1 }));
  return applyEvent(s, ev(sid, EventType.TOOL_APPROVAL_REQUESTED, APPROVAL_DATA));
}

// ── 渲染夹具（照抄 ApprovalModal.test.tsx 的最小宿主 + 假计时器） ──

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function renderTopBar(conversation: ConversationState): void {
  const ui: ReactElement = (
    <TopBar
      conversation={conversation}
      streaming
      inspectorOpen={false}
      onToggleInspector={() => {}}
      density={'balanced' as TraceDensity}
      onDensityChange={() => {}}
      theme={'dark' as Theme}
      onToggleTheme={() => {}}
      authRequired={false}
      onOpenMemories={() => {}}
      sessionId={null}
      onOpenContextUsage={() => {}}
    />
  );
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

const waitHint = () => container!.querySelector('.wait-hint');

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.useFakeTimers();
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  vi.useRealTimers();
});

describe('#443 — 顶栏等待提示的审批判据', () => {
  it('stale 审批 + 新 run 运行中：提示照出，但主语是模型（不说「等待审批」）', () => {
    renderTopBar(staleApprovalState('s-stale'));
    act(() => {
      vi.advanceTimersByTime(31_000);
    });
    // 提示本身必须在场——本票修的是主语说错，不是让提示消失
    expect(waitHint()).not.toBeNull();
    expect(waitHint()!.textContent).not.toContain('正在等待你的审批决定');
    expect(waitHint()!.textContent).toContain('仍在等待模型');
  });

  it('live 审批挂着：顶栏照说「正在等待你的审批决定」（防修过头）', () => {
    renderTopBar(liveApprovalState('s-live'));
    act(() => {
      vi.advanceTimersByTime(31_000);
    });
    expect(waitHint()).not.toBeNull();
    expect(waitHint()!.textContent).toContain('正在等待你的审批决定');
  });
});
