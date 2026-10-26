// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { PendingPlanApprovalRequest } from '../types';
import { PlanApprovalCard } from './PlanApprovalCard';

function request(overrides: Partial<PendingPlanApprovalRequest> = {}): PendingPlanApprovalRequest {
  return {
    request_id: 'plan-1',
    run_id: 'run-1',
    plan_markdown: '# 计划\n\n## 目标\n把 A 改成 B。\n\n## 分步改动\n1. 改 a.py',
    touched_files: ['src/a.py', 'tests/test_a.py'],
    answer: null,
    ...overrides,
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

const el = <T extends Element>(selector: string): T => {
  const node = document.querySelector(selector);
  if (!node) throw new Error(`missing ${selector}`);
  return node as T;
};
const all = (selector: string): HTMLElement[] =>
  Array.from(document.querySelectorAll(selector)) as HTMLElement[];
const click = (selector: string): void => {
  act(() => el<HTMLElement>(selector).click());
};
const type = (selector: string, value: string): void => {
  const input = el<HTMLTextAreaElement>(selector);
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
  act(() => {
    setter?.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  document.body.innerHTML = '';
});

describe('PlanApprovalCard', () => {
  it('渲染计划正文、触碰文件清单与三个裁决按钮', () => {
    render(<PlanApprovalCard request={request()} onSubmit={vi.fn()} />);

    expect(el('.plan-approval-status').textContent).toContain('等待批准');
    // 正文走 markdown 渲染（标题成元素、不是原样 # 号）。
    const plan = el('[data-testid="plan-approval-markdown"]');
    expect(plan.textContent).toContain('把 A 改成 B。');
    expect(plan.textContent).not.toContain('# 计划');

    const files = all('.plan-approval-file').map((n) => n.textContent);
    expect(files).toEqual(['src/a.py', 'tests/test_a.py']);
    expect(el('.plan-approval-files-title').textContent).toContain('2 个文件');

    expect(all('.plan-approval-btn')).toHaveLength(3);
  });

  it('三个按钮各点一次即提交对应裁决（AC9 的交互判据）', () => {
    const onSubmit = vi.fn();
    render(<PlanApprovalCard request={request()} onSubmit={onSubmit} />);

    click('.plan-approval-approve');
    click('.plan-approval-revise');
    click('.plan-approval-reject');

    expect(onSubmit.mock.calls.map((c) => c[0].decision)).toEqual([
      'approve', 'revise', 'reject',
    ]);
    for (const call of onSubmit.mock.calls) {
      expect(call[0].request_id).toBe('plan-1');
    }
  });

  it('反馈随 revise / reject 发出，但绝不随 approve 发出（后端对 approve+feedback 是 422）', () => {
    const onSubmit = vi.fn();
    render(<PlanApprovalCard request={request()} onSubmit={onSubmit} />);

    type('.plan-approval-feedback', '  先补测试  ');
    click('.plan-approval-revise');
    click('.plan-approval-reject');
    click('.plan-approval-approve');

    expect(onSubmit.mock.calls[0][0]).toEqual({
      request_id: 'plan-1', decision: 'revise', feedback: '先补测试',
    });
    expect(onSubmit.mock.calls[1][0]).toEqual({
      request_id: 'plan-1', decision: 'reject', feedback: '先补测试',
    });
    // approve 不带 feedback 字段（连空串都不带）。
    expect(onSubmit.mock.calls[2][0]).toEqual({ request_id: 'plan-1', decision: 'approve' });
    expect('feedback' in onSubmit.mock.calls[2][0]).toBe(false);
  });

  it('空白反馈不发送 feedback 字段', () => {
    const onSubmit = vi.fn();
    render(<PlanApprovalCard request={request()} onSubmit={onSubmit} />);

    type('.plan-approval-feedback', '   \n  ');
    click('.plan-approval-revise');

    expect(onSubmit.mock.calls[0][0]).toEqual({ request_id: 'plan-1', decision: 'revise' });
  });

  it('恢复在途时禁用全部三个按钮', () => {
    render(<PlanApprovalCard request={request()} onSubmit={vi.fn()} resuming />);

    for (const btn of all('.plan-approval-btn')) {
      expect((btn as HTMLButtonElement).disabled).toBe(true);
    }
  });

  it('恢复失败原文以 alert 呈现', () => {
    render(
      <PlanApprovalCard
        request={request()}
        onSubmit={vi.fn()}
        errorMessage="版本冲突，请刷新后重试"
      />,
    );

    expect(el('.plan-approval-error').getAttribute('role')).toBe('alert');
    expect(el('.plan-approval-error').textContent).toBe('版本冲突，请刷新后重试');
  });

  it('触碰文件为空时不渲染清单区（零伪造空列表）', () => {
    render(<PlanApprovalCard request={request({ touched_files: [] })} onSubmit={vi.fn()} />);

    expect(document.querySelector('.plan-approval-files')).toBeNull();
  });
});

describe('#1523 裁决提交后的焦点落点', () => {
  // 同 AskUserCard：以同 id 占位输入框代表真实 Composer（App 的既有焦点惯例）。
  const mountComposer = (): HTMLInputElement => {
    const composer = document.createElement('input');
    composer.id = 'composer-input';
    document.body.appendChild(composer);
    return composer;
  };

  it('裁决提交后焦点落到 composer 输入框（按钮禁用瞬间不悬空 body）', () => {
    const composer = mountComposer();
    const onSubmit = vi.fn();
    render(<PlanApprovalCard request={request()} onSubmit={onSubmit} />);

    click('.plan-approval-revise');
    expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ decision: 'revise' }));
    expect(document.activeElement).toBe(composer);
  });

  it('恢复失败（卡仍在）时焦点回到触发裁决的那个按钮，重试可继续', () => {
    const composer = mountComposer();
    render(<PlanApprovalCard request={request()} onSubmit={vi.fn()} />);
    click('.plan-approval-revise');
    expect(document.activeElement).toBe(composer);

    render(<PlanApprovalCard request={request()} errorMessage="版本冲突，请刷新后重试" onSubmit={vi.fn()} />);
    const active = document.activeElement as HTMLElement | null;
    expect(active?.classList.contains('plan-approval-revise')).toBe(true);
  });
});
