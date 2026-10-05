// @vitest-environment jsdom
/** #684：审批卡第三档「以后都允许」+ 两档粒度（F21 显式授权 / F22 无模糊匹配）。
 *
 *  生产可达性回归：第三档只在后端 requested 事件的 `allowed_decisions` 含
 *  `approve_policy` 时渲染；点击后展开精确/命令级二选一；选中即经 postApproval
 *  透传 `decision='approve_policy'` + `policy_granularity`。粒度不默认、不猜测。
 *
 *  宿主/驱动照抄 ApprovalCard.invalidSubmitted.test.tsx（jsdom + createRoot + act）。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, postApproval: vi.fn() };
});

import { postApproval } from '../lib/api';
import { ApprovalCard } from './ApprovalCard';
import type { PendingApproval } from '../types';

function approval(allowed: string[]): PendingApproval {
  return {
    approval_id: 'ap-1',
    tool_name: 'bash',
    tool_call_id: 'tc-1',
    action_type: 'danger',
    title: '执行命令',
    description: 'rm -rf 之类',
    arguments_preview: { command: 'ls' },
    permission: 'bash',
    policy: 'ask',
    reason: '危险操作',
    allowed_decisions: allowed,
  };
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
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  vi.mocked(postApproval).mockReset();
});

describe('ApprovalCard — 第三档「以后都允许」（#684）', () => {
  it('allowed_decisions 含 approve_policy ⇒ 渲染第三档按钮', () => {
    render(
      <ApprovalCard sessionId="s" approval={approval(['approve_once', 'deny', 'approve_policy'])} />,
    );
    const btn = query<HTMLButtonElement>('.approval-allow-policy');
    expect(btn).not.toBeNull();
    expect(btn!.textContent).toContain('以后都允许');
    // 未展开时无粒度选择器（粒度必须用户显式再选）。
    expect(query('.approval-policy-granularity')).toBeNull();
  });

  it('allowed_decisions 不含 approve_policy ⇒ 不渲染第三档（F21/F22）', () => {
    render(<ApprovalCard sessionId="s" approval={approval(['approve_once', 'deny'])} />);
    expect(query('.approval-allow-policy')).toBeNull();
  });

  it('点击第三档 ⇒ 展开精确/命令级选择器（不自动提交）', () => {
    render(
      <ApprovalCard sessionId="s" approval={approval(['approve_once', 'deny', 'approve_policy'])} />,
    );
    act(() => query<HTMLButtonElement>('.approval-allow-policy')!.click());
    expect(query('.approval-policy-exact')).not.toBeNull();
    expect(query('.approval-policy-command')).not.toBeNull();
    expect(postApproval).not.toHaveBeenCalled();
  });

  it('选择命令级 ⇒ postApproval(approve_policy, command) 透传粒度', async () => {
    vi.mocked(postApproval).mockResolvedValueOnce(undefined as never);
    render(
      <ApprovalCard sessionId="s" approval={approval(['approve_once', 'deny', 'approve_policy'])} />,
    );
    act(() => query<HTMLButtonElement>('.approval-allow-policy')!.click());
    await act(async () => {
      query<HTMLButtonElement>('.approval-policy-command')!.click();
    });
    expect(postApproval).toHaveBeenCalledWith('s', 'ap-1', true, 'approve_policy', 'command');
  });

  it('选择精确 ⇒ postApproval(approve_policy, exact) 透传粒度', async () => {
    vi.mocked(postApproval).mockResolvedValueOnce(undefined as never);
    render(
      <ApprovalCard sessionId="s" approval={approval(['approve_once', 'deny', 'approve_policy'])} />,
    );
    act(() => query<HTMLButtonElement>('.approval-allow-policy')!.click());
    await act(async () => {
      query<HTMLButtonElement>('.approval-policy-exact')!.click();
    });
    expect(postApproval).toHaveBeenCalledWith('s', 'ap-1', true, 'approve_policy', 'exact');
  });
});
