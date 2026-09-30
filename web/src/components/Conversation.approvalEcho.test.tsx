// @vitest-environment jsdom
/** #420 AC3 回显的接线测试：真投影状态 + 同一实例重渲染。
 *
 *  lib 契约（approvalEcho.test.ts）锁的是纯函数语义；这里锁**接线**——
 *  Conversation 在「挂起 → 决出」的真实重渲染序列里把回显卡渲染到 DOM 上，
 *  且锚定规则在组件边界依然成立（历史决策 / 换会话不回显）。
 *
 *  沿用 Conversation.memo.test.tsx 的做法：文件级 jsdom + 假 `useVirtualizer`
 *  （本文件不测虚拟化；回显卡渲染在虚拟化容器**之外**，`getVirtualItems: []`
 *  不影响它）。首次渲染即见待决卡 → ApprovalModal（Radix）会挂载，jsdom 可承。 */
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

// ── 投影夹具（事件形状与 lib/approvalEcho.test.ts 同源） ──

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

/** 一条有 turn 的会话 + 挂起中的审批（turn 非空才能过 Conversation 的空态早退）。 */
function pendingState(sid: string): ConversationState {
  let s = applyEvent(initConversation(sid), ev(sid, EventType.USER_MESSAGE, { content: '跑个命令', step: 1 }));
  s = applyEvent(s, ev(sid, EventType.RUN_STARTED, { turn_index: 1 }));
  return applyEvent(s, ev(sid, EventType.TOOL_APPROVAL_REQUESTED, APPROVAL_DATA));
}

/** 同一会话、审批已决（投影 approval_decisions 已有记录）。 */
function resolvedState(
  sid: string,
  decision = 'approve_once',
  reason = '用户批准',
): ConversationState {
  return applyEvent(
    pendingState(sid),
    ev(sid, EventType.PERMISSION_RESOLVED, { approval_id: 'ap-1', decision, reason }),
  );
}

// ── 渲染夹具（照抄 memo 测试的最小宿主） ──

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

const echoCards = () => container!.querySelectorAll('.approval-echo');
const echoByKey = (key: string) => container!.querySelector(`[data-approval-key="${key}"] .approval-echo`);

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
});

describe('#420 AC3 — Conversation 回显接线', () => {
  it('挂起阶段无回显；决出后同一实例重渲染 → 回显卡出现（消费投影记录）', () => {
    render(<Conversation conversation={pendingState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />);
    expect(echoCards()).toHaveLength(0); // 未决：回显是"结果落点"，不是第二张待决卡

    act(() => {
      root!.render(
        <Conversation conversation={resolvedState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />,
      );
    });
    expect(echoCards()).toHaveLength(1);
    expect(echoByKey('ap-1')).not.toBeNull();
    expect(container!.textContent).toContain('审批已决');
    expect(container!.textContent).toContain('批准（一次）');
    expect(container!.textContent).toContain('用户批准');
  });

  it('决出后再次重渲染：回显恰好一张（不随提交放大/消失）', () => {
    render(<Conversation conversation={pendingState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />);
    act(() => {
      root!.render(<Conversation conversation={resolvedState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />);
    });
    for (let i = 0; i < 3; i++) {
      act(() => {
        root!.render(<Conversation conversation={resolvedState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />);
      });
    }
    expect(echoCards()).toHaveLength(1);
  });

  it('换会话：新会话的历史决策（挂载前已决）不回显——seen 集合随 session_id 重置', () => {
    render(<Conversation conversation={pendingState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />);
    act(() => {
      root!.render(<Conversation conversation={resolvedState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />);
    });
    expect(echoCards()).toHaveLength(1); // 前提：sA 的回显在

    // 切到 sB：它在挂载前就有一条已决审批（deny）——对本观看窗是历史，不得回显
    act(() => {
      root!.render(
        <Conversation
          conversation={resolvedState('sB', 'deny', '超时')}
          loadingHistory={false}
          density={'balanced' as TraceDensity}
        />,
      );
    });
    expect(echoCards()).toHaveLength(0);
    expect(container!.textContent).not.toContain('审批已决');
  });

  it('首屏直接挂载已决会话（刷新场景）：无回显——回显是本观看窗内的活交互痕迹', () => {
    render(<Conversation conversation={resolvedState('sA')} loadingHistory={false} density={'balanced' as TraceDensity} />);
    expect(echoCards()).toHaveLength(0);
  });
});
