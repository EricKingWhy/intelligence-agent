// @vitest-environment jsdom
/** ApprovalModal 模态语义（#421，R5-B3 的修法）。
 *
 *  Radix Portal 在 renderToString（SSR 车道）里不渲染，本文件用文件级 jsdom
 *  （先例：Conversation.render.test.tsx）做真实客户端渲染——Portal 内容进
 *  document.body，「没有决策不许关」的 ESC 拦截与决策后的滞留行为都能真实驱动。
 *  浮层在真实浏览器里的视觉与遮挡（R5-B3 的原始症状），由 e2e 车道覆盖。 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, postApproval: vi.fn() };
});

import { postApproval } from '../lib/api';
import { ApprovalModal } from './ApprovalModal';
import type { PendingApproval } from '../types';

function approval(): PendingApproval {
  return {
    approval_id: 'ap-modal-1',
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

describe('ApprovalModal — #421 模态语义', () => {
  it('渲染进 portal：overlay + 卡片在场；没有关闭出口（没有决策不许关）', () => {
    render(<ApprovalModal sessionId="s" approval={approval()} />);
    expect(document.body.querySelector('.palette-overlay')).not.toBeNull();
    const content = document.body.querySelector('.approval-modal-content');
    expect(content).not.toBeNull();
    expect(content!.textContent).toContain('需要审批');
    // 仓库其余浮层用 aria-label="关闭" 的 X 按钮（DeleteSessionDialog）——
    // 审批浮层一个都不许有：关闭 = 后端确认决策，不是用户退出。
    expect(document.body.querySelector('[aria-label="关闭"]')).toBeNull();
    // dialog 语义由 Radix Content 承担；卡片让位（无 alertdialog 双重声明）
    expect(content!.getAttribute('role')).toBe('dialog');
    expect(document.body.querySelector('.approval-card')!.getAttribute('role')).toBeNull();
  });

  it('ESC 不关闭：卡片仍在（GitHub required-review 语义）', () => {
    render(<ApprovalModal sessionId="s" approval={approval()} />);
    act(() => {
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      );
    });
    expect(document.body.querySelector('.approval-modal-content')).not.toBeNull();
  });

  it('决策成功后进入「已提交」态（不自演已批准），浮层**不**自动撤——关闭由事件驱动', async () => {
    // #420 AC3：卡片不再本地伪造「已批准」——那由 permission/resolved 事件回答；
    // 本地只持有交互状态 submitted。浮层卸载同样由投影（候选资格）驱动。
    const onDecided = vi.fn();
    vi.mocked(postApproval).mockResolvedValueOnce(undefined as never);
    render(<ApprovalModal sessionId="s" approval={approval()} onDecided={onDecided} />);
    const approve = document.body.querySelector('.approval-approve') as HTMLButtonElement;
    await act(async () => {
      approve.click();
    });
    expect(postApproval).toHaveBeenCalledWith('s', 'ap-modal-1', true);
    expect(onDecided).toHaveBeenCalledTimes(1);
    const card = document.body.querySelector('.approval-card')!;
    expect(card.className).toContain('submitted');
    expect(card.textContent).toContain('决策已提交');
    expect(card.textContent).not.toContain('已批准');
    // 决策按钮撤走（防重复提交），但浮层仍在——候选（pending_approvals 成员）未变
    expect(document.body.querySelector('.approval-approve')).toBeNull();
    expect(document.body.querySelector('.approval-modal-content')).not.toBeNull();
  });
});
