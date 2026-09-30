// @vitest-environment jsdom
/** #444：「失效」与「已提交」同卡时的文案矛盾锁。
 *
 *  可达序列：POST 成功置 submitted（卡内交互状态，无 prop 可注入）→ 随后该审批
 *  被判失效（投影 stale 或 404-gone，invalid 由 App 统一计算后经 prop 下发）。
 *  标题三态互斥（invalid 优先）本来就对，但 submitted 说明文字没有 `!invalid` 门
 *  ⇒ 同一张卡同现「审批已失效」标题与「决策已提交」说明。
 *
 *  submitted 只能真驱动：mock postApproval 成功 → 点击批准 → 同一根重渲染加
 *  invalid（组件位置不变 ⇒ 内部 state 保留）。宿主照抄 ApprovalModal.test.tsx。
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

function approval(): PendingApproval {
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
    allowed_decisions: ['approve_once', 'deny'],
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

const card = () => document.body.querySelector('.approval-card')!;

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

describe('ApprovalCard — invalid 与 submitted 相遇（#444）', () => {
  it('已提交后再被判失效：只说「审批已失效」，不再说「决策已提交」', async () => {
    vi.mocked(postApproval).mockResolvedValueOnce(undefined as never);
    render(<ApprovalCard sessionId="s" approval={approval()} />);
    await act(async () => {
      (document.body.querySelector('.approval-approve') as HTMLButtonElement).click();
    });
    // 前置：确已进入 submitted 态（否则本测试没咬住目标状态）
    expect(card().className).toContain('submitted');
    expect(card().textContent).toContain('决策已提交');

    // 随后审批被判失效：同一实例重渲染，invalid prop 下发、内部 submitted 保留
    render(<ApprovalCard sessionId="s" approval={approval()} invalid />);
    expect(card().className).toContain('invalid');
    expect(card().textContent).toContain('审批已失效');
    expect(card().textContent).not.toContain('决策已提交');
  });
});
