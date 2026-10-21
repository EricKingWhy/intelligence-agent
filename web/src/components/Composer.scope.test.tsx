// @vitest-environment jsdom
/** #1191（Phase 2）：Composer 登记入口「作用域三选一」。
 *
 *  为什么必须真实客户端渲染：判据是勾选后**动态出现**的 pill 与选中态、以及点击切换
 *  后的 hint 文案——`renderToString` 只能看到未勾选的 SSR 形态（先例：
 *  `Composer.attachments.test.tsx` / `Composer.presetFocus.test.tsx`）。
 *
 *  锁：① 未勾选不出现三选一；② 勾选后默认「本会话」选中 + hint 逐字（票面 §四.1/§四.2）；
 *  ③ 切换选中项 → hint 逐字跟随；④ 非 git（projectAvailable=false）「本项目」置灰；
 *  ⑤ 提交时 scope 作为第 4 参交给提交口。 */

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, uploadAttachment: vi.fn() };
});

import type { ConstraintScope } from '../lib/api';
import { Composer } from './Composer';

type SubmitArgs = [
  task: string,
  rememberAsProceduralRule: boolean,
  attachmentIds?: string[],
  scope?: ConstraintScope,
];

let container: HTMLDivElement;
let root: Root;
let onSubmit: Mock<(...args: SubmitArgs) => void>;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onSubmit = vi.fn<(...args: SubmitArgs) => void>();
  // Radix DropdownMenu（#1405 附件菜单）依赖 ResizeObserver；jsdom 没有。
  vi.stubGlobal('ResizeObserver', class {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  });
  Element.prototype.scrollIntoView = () => {};
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.body.innerHTML = '';
});

function paint(projectAvailable: boolean | null = null): void {
  act(() => {
    root.render(
      createElement(Composer, {
        streaming: false,
        onSubmit,
        onCancel: () => {},
        sessionId: 'sess-1',
        onEnsureSession: async () => 'sess-new',
        models: [],
        selectedModel: null,
        projectAvailable,
      }),
    );
  });
}

const addTrigger = (): HTMLButtonElement =>
  container.querySelector<HTMLButtonElement>('button[aria-label="添加图片"]')!;
/** #1405：规则开关在附件「+」菜单里，portal 渲染到 document.body。 */
const ruleItem = (): HTMLElement | null =>
  document.querySelector<HTMLElement>('#remember-as-procedural-rule');
function toggleRule(): void {
  act(() => {
    const trigger = addTrigger();
    trigger.focus();
    trigger.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    );
  });
  expect(ruleItem()).not.toBeNull();
  act(() => {
    ruleItem()!.click();
  });
}
const pills = (): HTMLButtonElement[] =>
  [...container.querySelectorAll<HTMLButtonElement>('button.composer-scope-pill')];
const pillByText = (label: string): HTMLButtonElement => {
  const found = pills().find((b) => (b.textContent ?? '').trim() === label);
  if (!found) throw new Error(`找不到 scope pill：${label}`);
  return found;
};
const hintText = (): string =>
  container.querySelector('.composer-scope-hint-text')?.textContent ?? '';

async function typeText(text: string): Promise<void> {
  await act(async () => {
    const ta = container.querySelector<HTMLTextAreaElement>('#composer-input')!;
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      'value',
    )?.set;
    setter?.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function pressEnter(): void {
  const event = new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  act(() => {
    container.querySelector<HTMLTextAreaElement>('#composer-input')!.dispatchEvent(event);
  });
}

describe('Composer 登记作用域（#1191 Phase 2）', () => {
  it('#1405：规则开关只在附件菜单里出现，不常驻 dock', () => {
    paint();
    expect(ruleItem()).toBeNull();
    act(() => {
      const trigger = addTrigger();
      trigger.focus();
      trigger.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
      );
    });
    expect(ruleItem()).not.toBeNull();
    expect(ruleItem()!.getAttribute('role')).toBe('menuitemcheckbox');
    expect(ruleItem()!.getAttribute('aria-checked')).toBe('false');
  });

  it('未勾选：不渲染三选一与 hint', () => {
    paint();
    expect(pills().length).toBe(0);
    expect(container.querySelector('.composer-scope-hint')).toBeNull();
  });

  it('勾选后出现三选一，默认「本会话」选中，hint 逐字（票面 §四.1/§四.2）', () => {
    paint();
    toggleRule();
    expect(pills().map((b) => (b.textContent ?? '').trim())).toEqual([
      '本会话',
      '本项目',
      '我的全局',
    ]);
    expect(pillByText('本会话').getAttribute('aria-pressed')).toBe('true');
    expect(pillByText('本项目').getAttribute('aria-pressed')).toBe('false');
    expect(hintText()).toBe('只在当前会话生效，随会话结束自然失效。适合一次性的要求。');
  });

  it('切换选中项 → hint 逐字跟随', () => {
    paint();
    toggleRule();
    act(() => pillByText('本项目').click());
    expect(hintText()).toBe('写入项目级存储，本仓库开的所有会话都生效，别的项目不受影响。');
    act(() => pillByText('我的全局').click());
    expect(hintText()).toBe('写入用户级存储，所有项目的新会话都生效。适合长期习惯。');
  });

  it('非 git（projectAvailable=false）→ 「本项目」置灰不可选（票面 §七.4）', () => {
    paint(false);
    toggleRule();
    expect(pillByText('本项目').disabled).toBe(true);
    expect(pillByText('本会话').disabled).toBe(false);
    expect(pillByText('我的全局').disabled).toBe(false);
  });

  it('提交：默认「本会话」→ scope=session', async () => {
    paint(true);
    toggleRule();
    await typeText('记住：TDD 先行');
    pressEnter();
    expect(onSubmit).toHaveBeenLastCalledWith('记住：TDD 先行', true, [], 'session');
  });

  it('提交：选「本项目」→ scope=project', async () => {
    paint(true);
    toggleRule();
    act(() => pillByText('本项目').click());
    await typeText('本项目都要 TDD');
    pressEnter();
    expect(onSubmit).toHaveBeenLastCalledWith('本项目都要 TDD', true, [], 'project');
  });
});
