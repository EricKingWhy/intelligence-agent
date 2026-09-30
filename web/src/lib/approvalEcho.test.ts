/** approvalEcho 契约测试——#420 AC3 回显的锚定规则。
 *
 *  锁四条边界：
 *  1. 回显**只**来自投影 `approval_decisions`（消费投影，不本地伪造）；
 *  2. 锚 = 「本次观看期间见过它挂起」——历史决策（挂载前就存在）不回显；
 *  3. 见过但未决（还在 pending）不回显——回显是"结果落点"，不是第二张待决卡；
 *  4. 登记幂等：同一 id 重复登记不产生任何放大。
 *
 *  组件接线（Conversation 渲染回显卡）由 `Conversation.approvalEcho.test.tsx`
 *  用真投影状态 + 重渲染覆盖；本文件只锁纯函数语义。 */

import { describe, expect, it } from 'vitest';
import { applyEvent, initConversation } from './projection';
import { EventType, type AgentEvent, type EventTypeValue } from '../types';
import { collectApprovalEchoes, notePendingApprovals } from './approvalEcho';

let seq = 0;
function ev(type: EventTypeValue, data: Record<string, unknown>): AgentEvent {
  seq += 1;
  return { session_id: 's', time: '2026-09-30T00:00:00Z', type, seq, run_id: 'r', step_id: 2, data };
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

function stateWithPending() {
  const s = applyEvent(initConversation('s'), ev(EventType.RUN_STARTED, { turn_index: 1 }));
  return applyEvent(s, ev(EventType.TOOL_APPROVAL_REQUESTED, APPROVAL_DATA));
}

function stateWithResolved() {
  const s = stateWithPending();
  return applyEvent(
    s,
    ev(EventType.PERMISSION_RESOLVED, { approval_id: 'ap-1', decision: 'approve_once', reason: '用户批准' }),
  );
}

describe('notePendingApprovals — seen 集合登记', () => {
  it('把当前待决审批的 id 登记进集合', () => {
    const seen = new Set<string>();
    notePendingApprovals(seen, stateWithPending());
    expect(seen.has('ap-1')).toBe(true);
  });

  it('幂等：重复登记不放大', () => {
    const seen = new Set<string>();
    const state = stateWithPending();
    notePendingApprovals(seen, state);
    notePendingApprovals(seen, state);
    expect(seen.size).toBe(1);
  });
});

describe('collectApprovalEchoes — 回显锚定', () => {
  it('见过且已决 → 回显（消费投影记录，含 decision/reason/tool_name）', () => {
    const seen = new Set<string>();
    notePendingApprovals(seen, stateWithPending());
    const echoes = collectApprovalEchoes(seen, stateWithResolved());
    expect(echoes).toHaveLength(1);
    expect(echoes[0]).toMatchObject({
      approval_id: 'ap-1',
      decision: 'approve_once',
      reason: '用户批准',
      tool_name: 'bash',
    });
  });

  it('挂载前就存在的决策（历史）不回显——seen 集合为空就是空', () => {
    const echoes = collectApprovalEchoes(new Set<string>(), stateWithResolved());
    expect(echoes).toEqual([]);
  });

  it('见过但未决（仍 pending）不回显', () => {
    const seen = new Set<string>();
    notePendingApprovals(seen, stateWithPending());
    expect(collectApprovalEchoes(seen, stateWithPending())).toEqual([]);
  });

  it('全会话的其它历史决策不因登记了别的 id 而混入', () => {
    const seen = new Set<string>();
    notePendingApprovals(seen, stateWithPending());
    // 再决一条**从未见过**的审批 ap-2（别的观看窗里发生的）
    const s = applyEvent(
      stateWithResolved(),
      ev(EventType.TOOL_APPROVAL_REQUESTED, { ...APPROVAL_DATA, approval_id: 'ap-2' }),
    );
    const withSecond = applyEvent(
      s,
      ev(EventType.PERMISSION_RESOLVED, { approval_id: 'ap-2', decision: 'deny', reason: '超时' }),
    );
    const echoes = collectApprovalEchoes(seen, withSecond);
    expect(echoes.map((d) => d.approval_id)).toEqual(['ap-1']);
  });
});
