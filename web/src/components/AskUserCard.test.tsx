// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { AskUserAnswer, PendingAskUserRequest } from '../types';
import { AskUserCard } from './AskUserCard';

function request(overrides: Partial<PendingAskUserRequest> = {}): PendingAskUserRequest {
  return {
    request_id: 'ask-1',
    run_id: 'run-1',
    questions: [
      {
        question: '新版本什么时候发布？',
        header: '发布决策',
        recommended: 0,
        options: [
          { label: '今晚低峰期', description: '23:00 后流量最低。' },
          { label: '明早 9 点', description: '团队都在工位。' },
          { label: '等测试全绿再定' },
        ],
      },
      {
        question: '这次发版走哪套部署流程？',
        header: '发布决策',
        options: [
          { label: '蓝绿部署', description: '零停机切换。' },
          { label: '滚动更新' },
        ],
      },
    ],
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

const click = (selector: string, index = 0): void => {
  act(() => all(selector)[index].click());
};
const type = (selector: string, value: string): void => {
  const input = el<HTMLTextAreaElement>(selector);
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
  act(() => {
    setter?.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};
const submit = (): void => click('.ask-user-submit');

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

describe('AskUserCard', () => {
  it('renders the status line, scope chip, title and numbered options with a recommended badge', () => {
    render(<AskUserCard request={request()} onSubmit={vi.fn()} />);

    expect(el('.ask-user-status').textContent).toContain('提问 · 等待回答');
    expect(el('.ask-user-scope').textContent).toBe('发布决策');
    expect(el('.ask-user-title').textContent).toBe('新版本什么时候发布？');

    const opts = all('.ask-user-opt');
    expect(opts).toHaveLength(3);
    expect(opts[0].querySelector('.ask-user-num')?.textContent).toBe('1');
    expect(opts[0].textContent).toContain('今晚低峰期');
    expect(opts[0].textContent).toContain('23:00 后流量最低。');
    // 推荐标只挂在 recommended 指向的那个选项上。
    expect(all('.ask-user-rec')).toHaveLength(1);
    expect(opts[0].querySelector('.ask-user-rec')?.textContent).toBe('推荐');
    expect(el('.ask-user-page').textContent).toBe('1 / 2');
  });

  it('highlights the selected option and preserves it across paging, then submits labels', () => {
    const onSubmit = vi.fn();
    render(<AskUserCard request={request()} onSubmit={onSubmit} />);

    click('.ask-user-opt', 0);
    const selected = all('.ask-user-opt').filter((o) => o.classList.contains('selected'));
    expect(selected).toHaveLength(1);
    expect(selected[0].textContent).toContain('今晚低峰期');

    click('[aria-label="下一题"]');
    expect(el('.ask-user-title').textContent).toBe('这次发版走哪套部署流程？');
    click('.ask-user-opt', 0);
    click('[aria-label="上一题"]');
    expect(el('.ask-user-title').textContent).toBe('新版本什么时候发布？');
    expect(all('.ask-user-opt').filter((o) => o.classList.contains('selected'))).toHaveLength(1);

    submit();
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const answer = onSubmit.mock.calls[0][0] as AskUserAnswer;
    expect(answer.request_id).toBe('ask-1');
    expect(answer.questions).toEqual([
      { question: '新版本什么时候发布？', answer: { label: '今晚低峰期' } },
      { question: '这次发版走哪套部署流程？', answer: { label: '蓝绿部署' } },
    ]);
  });

  it('opens the custom input and submits typed text as custom_text', () => {
    const onSubmit = vi.fn();
    render(<AskUserCard request={request()} onSubmit={onSubmit} />);

    expect(document.querySelector('.ask-user-custom-input')).toBeNull();
    click('.ask-user-custom-row');
    expect(el('.ask-user-custom-input').tagName).toBe('TEXTAREA');
    expect(el('.ask-user-custom-row').classList.contains('selected')).toBe(true);

    type('.ask-user-custom-input', '下周一凌晨');
    click('[aria-label="下一题"]');
    click('.ask-user-opt', 1);
    submit();

    const answer = onSubmit.mock.calls[0][0] as AskUserAnswer;
    expect(answer.questions[0]).toEqual({
      question: '新版本什么时候发布？',
      answer: { custom_text: '下周一凌晨' },
    });
    expect(answer.questions[1]).toEqual({
      question: '这次发版走哪套部署流程？',
      answer: { label: '滚动更新' },
    });
  });

  it('skips a single question (留空) and still allows submitting the rest', () => {
    const onSubmit = vi.fn();
    render(<AskUserCard request={request()} onSubmit={onSubmit} />);

    click('.ask-user-skip-question');
    expect(el('.ask-user-title').textContent).toBe('这次发版走哪套部署流程？');
    click('.ask-user-opt', 0);
    submit();

    const answer = onSubmit.mock.calls[0][0] as AskUserAnswer;
    expect(answer.questions[0]).toEqual({ question: '新版本什么时候发布？', answer: null });
    expect(answer.questions[1]).toEqual({
      question: '这次发版走哪套部署流程？',
      answer: { label: '蓝绿部署' },
    });
  });

  // 审查 P4-4：末题「跳过此题」的用户可见分支（`index < lastIndex` 为假 ⇒ 不再前进）。
  it('skipping the last question clears it and stays on the last page', () => {
    const onSubmit = vi.fn();
    render(<AskUserCard request={request()} onSubmit={onSubmit} />);

    click('.ask-user-opt', 0); // 第 1 题作答 → 解锁下一页
    click('[aria-label="下一题"]');
    expect(el('.ask-user-title').textContent).toBe('这次发版走哪套部署流程？');

    click('.ask-user-skip-question'); // 末题跳过：页码与题干都不动
    expect(el('.ask-user-title').textContent).toBe('这次发版走哪套部署流程？');
    expect(el('.ask-user-page').textContent).toBe('2 / 2');

    submit();
    const answer = onSubmit.mock.calls[0][0] as AskUserAnswer;
    expect(answer.questions[0]).toEqual({
      question: '新版本什么时候发布？',
      answer: { label: '今晚低峰期' },
    });
    expect(answer.questions[1]).toEqual({
      question: '这次发版走哪套部署流程？',
      answer: null,
    });
  });

  it('submits all-null answers when the whole card is skipped', () => {
    const onSubmit = vi.fn();
    render(<AskUserCard request={request()} onSubmit={onSubmit} />);

    click('.ask-user-skip');
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(onSubmit.mock.calls[0][0]).toEqual({
      request_id: 'ask-1',
      questions: [
        { question: '新版本什么时候发布？', answer: null },
        { question: '这次发版走哪套部署流程？', answer: null },
      ],
    });
  });

  it('counts an empty custom row as unanswered (null) rather than an answer', () => {
    const onSubmit = vi.fn();
    render(<AskUserCard request={request()} onSubmit={onSubmit} />);

    click('.ask-user-custom-row');
    submit();
    const answer = onSubmit.mock.calls[0][0] as AskUserAnswer;
    expect(answer.questions[0]).toEqual({ question: '新版本什么时候发布？', answer: null });
  });

  it('disables submit while resuming', () => {
    render(<AskUserCard request={request()} resuming onSubmit={vi.fn()} />);
    expect(el<HTMLButtonElement>('.ask-user-submit').disabled).toBe(true);
  });

  it('shows the timeout tip with configured minutes, and a neutral tip when unknown', () => {
    render(<AskUserCard request={request()} timeoutMinutes={5} onSubmit={vi.fn()} />);
    expect(el('.ask-user-timeout-tip').textContent).toContain('5 分钟');

    render(<AskUserCard request={request()} onSubmit={vi.fn()} />);
    const tip = el('.ask-user-timeout-tip').textContent ?? '';
    expect(tip).toContain('自动继续');
    expect(tip).not.toContain('分钟');
  });

  it('collapses the body via the collapse button while keeping the header', () => {
    render(<AskUserCard request={request()} onSubmit={vi.fn()} />);
    click('[aria-label="收起"]');
    expect(el('.ask-user-card').classList.contains('collapsed')).toBe(true);
    expect(el('.ask-user-scope').textContent).toBe('发布决策');
    expect(el('.ask-user-body').hasAttribute('hidden')).toBe(true);
    expect(el('.ask-user-foot').hasAttribute('hidden')).toBe(true);
  });

  // 审查 P4-4：整卡收起（`closed`）的用户可见分支与「展开提问」恢复路径。
  it('closes the whole card and restores it via 展开提问', () => {
    render(<AskUserCard request={request()} onSubmit={vi.fn()} />);
    expect(document.querySelector('.ask-user-closed')).toBeNull();

    click('[aria-label="关闭"]');
    // 收起态：状态行仍在，但题干 / 选项 / 提交行都不再渲染。
    expect(el('.ask-user-status').textContent).toContain('提问 · 等待回答');
    expect(el('.ask-user-closed').textContent).toContain('提问卡片已收起');
    expect(document.querySelector('.ask-user-title')).toBeNull();
    expect(all('.ask-user-opt')).toHaveLength(0);
    expect(document.querySelector('.ask-user-submit')).toBeNull();

    click('.ask-user-closed .ask-user-btn'); // 展开提问
    expect(document.querySelector('.ask-user-closed')).toBeNull();
    expect(el('.ask-user-title').textContent).toBe('新版本什么时候发布？');
    expect(all('.ask-user-opt')).toHaveLength(3);
  });

  it('surfaces a resume error message', () => {
    render(<AskUserCard request={request()} errorMessage="恢复被拒绝：..." onSubmit={vi.fn()} />);
    expect(el('[role="alert"]').textContent).toContain('恢复被拒绝');
  });
});

describe('#1523 最终提交后的焦点落点', () => {
  // App 的既有焦点惯例（App.tsx `document.getElementById('composer-input')?.focus()`）：
  // 测试里挂一个同 id 的占位输入框代表真实 Composer；jsdom 的 click 不做默认聚焦，
  // activeElement 的任何变化都只能来自组件自身的焦点处理。
  const mountComposer = (): HTMLInputElement => {
    const composer = document.createElement('input');
    composer.id = 'composer-input';
    document.body.appendChild(composer);
    return composer;
  };

  it('提交后焦点落到明确可操作位置（composer 输入框），不是悬空 body', () => {
    const composer = mountComposer();
    const onSubmit = vi.fn();
    render(<AskUserCard request={request()} onSubmit={onSubmit} />);

    click('.ask-user-submit');
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(document.activeElement).toBe(composer);
  });

  it('整卡跳过同样落焦 composer', () => {
    const composer = mountComposer();
    render(<AskUserCard request={request()} onSubmit={vi.fn()} />);

    click('.ask-user-skip');
    expect(document.activeElement).toBe(composer);
  });

  it('恢复失败（卡仍在）时焦点回到触发提交的那个按钮，失败重试可继续', () => {
    const composer = mountComposer();
    render(<AskUserCard request={request()} onSubmit={vi.fn()} />);
    click('.ask-user-skip');
    expect(document.activeElement).toBe(composer);

    // App 的失败路径：resuming 结束、卡片仍挂载并带错误原文。
    render(<AskUserCard request={request()} errorMessage="恢复失败：网络中断" onSubmit={vi.fn()} />);
    const active = document.activeElement as HTMLElement | null;
    expect(active?.classList.contains('ask-user-skip')).toBe(true);
  });
});
