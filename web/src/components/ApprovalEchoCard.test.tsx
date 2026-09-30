/** ApprovalEchoCard 渲染契约（#420 AC3 回显）。
 *
 *  静态渲染（renderToStaticMarkup，同 ApprovalCard.test.tsx 的口径）。锁四条：
 *  1. 措辞复用 `lib/permission.decisionLabel`（approve_once → 批准（一次））；
 *  2. 语义色按 `verdictTone`（绿=已批准是断言；未知值 neutral，不得画成批准样）；
 *  3. reason 有则显示（fail-closed 超时拒因要能看到）、无则不留空壳；
 *  4. tool_name 配不上对（事件窗口从中间开始）时不编造。 */
import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { ApprovalEchoCard } from './ApprovalEchoCard';
import type { ApprovalDecision } from '../types';

function decision(overrides: Partial<ApprovalDecision>): ApprovalDecision {
  return {
    approval_id: 'ap-1',
    decision: 'approve_once',
    reason: '用户批准',
    tool_name: 'bash',
    ...overrides,
  };
}

describe('ApprovalEchoCard — 审批结果回显（#420 AC3）', () => {
  it('approve_once → 「审批已决：批准（一次）」+ allow 色 + 原因', () => {
    const html = renderToStaticMarkup(<ApprovalEchoCard decision={decision({})} />);
    expect(html).toContain('审批已决');
    expect(html).toContain('批准（一次）');
    expect(html).toContain('approval-echo allow');
    expect(html).toContain('用户批准');
    expect(html).toContain('bash');
  });

  it('deny → 「拒绝」+ deny 色 + 超时拒因可见（fail-closed 留痕）', () => {
    const html = renderToStaticMarkup(
      <ApprovalEchoCard
        decision={decision({ decision: 'deny', reason: '审批超时（300s 无决策），按 fail-closed 拒绝' })}
      />,
    );
    expect(html).toContain('approval-echo deny');
    expect(html).toContain('拒绝');
    expect(html).toContain('审批超时（300s 无决策），按 fail-closed 拒绝');
    expect(html).not.toContain('approval-echo allow');
  });

  it('未知决策原样显示 + neutral（缺字段的 permission/resolved 不得被画成已批准）', () => {
    const html = renderToStaticMarkup(
      <ApprovalEchoCard decision={decision({ decision: 'weird_new_granularity', reason: '' })} />,
    );
    expect(html).toContain('weird_new_granularity');
    expect(html).toContain('approval-echo neutral');
    expect(html).not.toContain('approval-echo allow');
  });

  it('reason 缺失 → 不留空壳；tool_name 配不上对 → 不编造', () => {
    const html = renderToStaticMarkup(
      <ApprovalEchoCard decision={decision({ reason: '', tool_name: undefined })} />,
    );
    expect(html).not.toContain('approval-echo-reason');
    expect(html).not.toContain('<code');
  });
});
