// @vitest-environment jsdom
/** #1177 §B：新建任务对话框化简（结构/折叠/按钮）。
 *
 *  规则（票面 §B + 效果图 02/03）：
 *  - 任务描述是唯一主视觉；空描述时「创建并启动」不可用（空描述拦截）；
 *  - 完成目标 / 自主度 / 模型 / 运行位置收进「高级设置」，**默认折叠**；
 *  - 底部按钮：取消 / 创建并启动（「创建即启动」语义不变，这里是文案与结构）。
 *
 *  本文件是 `TaskCreationDialog.sandboxOptions.test.tsx` 的结构姐妹篇：那个文件锁
 *  运行位置后端探针契约，本文件锁化简后的层级与折叠行为。
 */
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';

import type {
  ModelCatalogEntry,
  SandboxBackendEntry,
  StartSessionFailure,
  StartSessionPayload,
  SubmitTaskResult,
} from '../lib/api';
import {
  LOCAL_BACKEND_NTFS_NOTICE,
  LOCAL_BACKEND_NOTICE,
  TaskCreationDialog,
} from './TaskCreationDialog';
import type { Project } from '../types';

const project: Project = {
  id: 'p1',
  path: 'D:\\Projects\\Demo',
  title: 'Demo',
  status: 'ok',
  session_ids: [],
  created_at: '2026-10-13T00:00:00Z',
  updated_at: '2026-10-13T00:00:00Z',
};

const backend = (name: string, available = true): SandboxBackendEntry => ({
  backend: name,
  available,
  reason: available ? null : '不可用原因',
  details: {},
});

const models: ModelCatalogEntry[] = [
  { name: 'glm-x', provider: 'glm', model: 'glm-x', default: true },
];

let container: HTMLDivElement | null = null;
let root: Root | null = null;
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

function render(ui: ReactElement): void {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root!.render(ui));
}

const byText = (selector: string) => document.body.querySelector<HTMLElement>(selector);
const advanced = () => byText('#task-advanced')!;
const advancedToggle = () =>
  document.body.querySelector<HTMLButtonElement>('.project-advanced-toggle')!;

function dialog(over: {
  project?: Project;
  onCreateTask?: (p: StartSessionPayload) => Promise<SubmitTaskResult>;
  onOpenChange?: (o: boolean) => void;
} = {}) {
  return (
    <TaskCreationDialog
      project={over.project ?? project}
      models={models}
      sandboxBackends={[backend('local'), backend('docker')]}
      onOpenChange={over.onOpenChange ?? (() => {})}
      onCreateTask={over.onCreateTask ?? (async () => null)}
    />
  );
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    unobserve() {}
    disconnect() {}
  });
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
    configurable: true,
    value: () => {},
  });
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  vi.unstubAllGlobals();
  if (originalScrollIntoView) {
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true,
      value: originalScrollIntoView,
    });
  } else {
    Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
  }
});

describe('TaskCreationDialog 化简（#1177 §B）', () => {
  it('a long project path can be revealed with a keyboard-focusable control', async () => {
    const longPath = 'C:\\workspace\\projects\\team\\service\\very-long-working-directory';
    await act(async () => {
      render(dialog({ project: { ...project, path: longPath } }));
      await Promise.resolve();
    });

    const toggle = document.body.querySelector<HTMLButtonElement>('[aria-controls="project-path-full"]');
    expect(toggle).not.toBeNull();
    expect(toggle?.tagName).toBe('BUTTON');
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
    const fullPath = document.body.querySelector<HTMLElement>('#project-path-full');
    expect(fullPath?.hidden).toBe(true);
    toggle?.focus();
    expect(document.activeElement).toBe(toggle);

    await act(async () => {
      toggle?.click();
    });
    expect(toggle?.getAttribute('aria-expanded')).toBe('true');
    expect(fullPath?.hidden).toBe(false);
    expect(fullPath?.textContent).toBe(longPath);
  });

  it('高级设置默认折叠：完成目标/自主度/模型/运行位置收进折叠容器', async () => {
    await act(async () => {
      render(dialog());
      await Promise.resolve();
    });

    const toggle = advancedToggle();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(advanced().hidden).toBe(true);
    // 折叠字段仍在 DOM 中，但整块 hidden（不进可访问性树、不渲染）——JSDOM 不应用
    // CSS，"不可见"由 hidden 属性间接表达，真正的可见性由 e2e 兜。
    // 四个字段都必须**归属**折叠容器：只锁「运行位置」会让"把完成目标 / 模型移出折叠区
    // 改成常显"仍然全绿（验收 2「其余四者收进折叠」的核心意图回退测不出），故逐一断言。
    expect(advanced().contains(document.body.querySelector('#task-acceptance'))).toBe(true);
    expect(advanced().contains(document.body.querySelector('#autonomy-label'))).toBe(true);
    expect(advanced().contains(document.body.querySelector('[aria-label="模型"]'))).toBe(true);
    expect(advanced().contains(document.body.querySelector('[aria-label="运行位置"]'))).toBe(true);
  });

  it('点击高级设置展开：完成目标/自主度/模型/运行位置出现；再点收起', async () => {
    await act(async () => {
      render(dialog());
      await Promise.resolve();
    });

    const toggle = advancedToggle();
    await act(async () => {
      toggle.click();
    });
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(advanced().hidden).toBe(false);
    expect(document.body.querySelector('label[for="task-acceptance"]')).not.toBeNull();
    expect(document.body.querySelector('#autonomy-label')).not.toBeNull();
    expect(document.body.querySelector('[aria-label="模型"]')).not.toBeNull();

    await act(async () => {
      toggle.click();
    });
    expect(advanced().hidden).toBe(true);
  });

  it('完成目标：单行 input[type=text] + placeholder「什么结果算完成？」（#1177 效果图 03）', async () => {
    await act(async () => {
      render(dialog());
      await Promise.resolve();
    });
    await act(async () => {
      advancedToggle().click();
    });

    // #1177 §B 效果图 03 / 用户 2026-10-17 裁决 1A：完成目标是**单行** input，
    // 不是多行 textarea（对照 `mockup.html:669` 的 `<input id="goal" type="text">`）。
    const goal = document.body.querySelector('#task-acceptance')!;
    expect(goal.tagName).toBe('INPUT');
    expect(goal.getAttribute('type')).toBe('text');
    expect(goal.getAttribute('placeholder')).toBe('什么结果算完成？');
    // a11y 不退：label 仍按 for/id 关联到这个单行输入（键盘可达、可访问名称不变）。
    expect(document.body.querySelector('label[for="task-acceptance"]')).not.toBeNull();
  });

  it('底部按钮是 取消 / 创建并启动；空描述时创建并启动禁用', async () => {
    await act(async () => {
      render(dialog());
      await Promise.resolve();
    });

    const cancel = [...document.body.querySelectorAll('button')].find(
      (b) => b.textContent === '取消',
    );
    const create = [...document.body.querySelectorAll('button')].find(
      (b) => b.textContent === '创建并启动',
    );
    expect(cancel).toBeTruthy();
    expect(create).toBeTruthy();
    expect(create!.disabled).toBe(true); // 空描述拦截

    const prompt = document.body.querySelector<HTMLTextAreaElement>('#task-prompt')!;
    await act(async () => {
      // React 受控输入：用原生 setter 触发 onChange。
      const setter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        'value',
      )!.set!;
      setter.call(prompt, '给登录接口加上限流');
      prompt.dispatchEvent(new Event('input', { bubbles: true }));
    });
    expect(create!.disabled).toBe(false);
  });

  it('创建并启动：payload 带 task/cwd/autonomy；完成目标进 criteria 字段、不拼进 prompt；创建成功后请求关闭', async () => {
    const onCreateTask = vi.fn(async (_p: unknown) => null);
    const onOpenChange = vi.fn();
    await act(async () => {
      render(dialog({ onCreateTask, onOpenChange }));
      await Promise.resolve();
    });

    const prompt = document.body.querySelector<HTMLTextAreaElement>('#task-prompt')!;
    // 受控输入的 value setter 在各自原型上：任务描述是 textarea，完成目标是单行 input。
    const setValue = (el: HTMLInputElement | HTMLTextAreaElement, v: string) => {
      const proto = el.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(proto, 'value')!.set!;
      setter.call(el, v);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    };

    await act(async () => {
      setValue(prompt, '跑一遍回归测试');
      advancedToggle().click();
    });
    const acceptance = document.body.querySelector<HTMLInputElement>('#task-acceptance')!;
    await act(async () => {
      setValue(acceptance, 'pytest 全绿');
    });

    const create = [...document.body.querySelectorAll('button')].find(
      (b) => b.textContent === '创建并启动',
    )!;
    await act(async () => {
      create.click();
      await Promise.resolve();
    });

    expect(onCreateTask).toHaveBeenCalledTimes(1);
    const payload = onCreateTask.mock.calls[0][0] as Record<string, unknown>;
    // #1359 阶段 2：完成目标是结构化字段，task 保持 prompt 原文（不再有
    // 「完成目标：…\n\n…」拼接——拼接会让验收项在事件流里不可检索）。
    expect(payload.task).toBe('跑一遍回归测试');
    expect(payload.criteria).toEqual([{ text: 'pytest 全绿' }]);
    expect(payload.cwd).toBe('D:\\Projects\\Demo');
    expect(payload.autonomy).toBe('ask'); // 默认档
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it('取消：不创建，请求关闭', async () => {
    const onCreateTask = vi.fn(async (_p: unknown) => null);
    const onOpenChange = vi.fn();
    await act(async () => {
      render(dialog({ onCreateTask, onOpenChange }));
      await Promise.resolve();
    });

    const cancel = [...document.body.querySelectorAll('button')].find(
      (b) => b.textContent === '取消',
    )!;
    await act(async () => {
      cancel.click();
    });
    expect(onCreateTask).not.toHaveBeenCalled();
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it('Esc 关闭：Escape 键请求关闭（Radix Dialog 默认，验收 4）', async () => {
    const onOpenChange = vi.fn();
    await act(async () => {
      render(dialog({ onOpenChange }));
      await Promise.resolve();
    });

    // Radix Dialog 的 DismissableLayer 在 document 上监听 keydown；Escape 关窗是它的
    // 默认行为，这里锁住它确实透传到 onOpenChange(false)（确认面可键盘关闭）。
    await act(async () => {
      document.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }),
      );
    });
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });
});

/** #1644：填 prompt 并点「创建并启动」（结构化回执用例的公共前半段）。 */
async function submitPrompt(
  onCreateTask: (p: StartSessionPayload) => Promise<SubmitTaskResult>,
): Promise<{ onOpenChange: Mock<(open: boolean) => void> }> {
  const onOpenChange = vi.fn();
  await act(async () => {
    render(dialog({ onCreateTask, onOpenChange }));
    await Promise.resolve();
  });
  const prompt = document.body.querySelector<HTMLTextAreaElement>('#task-prompt')!;
  await act(async () => {
    const setter = Object.getOwnPropertyDescriptor(
      HTMLTextAreaElement.prototype,
      'value',
    )!.set!;
    setter.call(prompt, '给登录接口加上限流');
    prompt.dispatchEvent(new Event('input', { bubbles: true }));
  });
  const create = [...document.body.querySelectorAll('button')].find(
    (b) => b.textContent === '创建并启动',
  )!;
  await act(async () => {
    create.click();
    await Promise.resolve();
  });
  return { onOpenChange };
}

const sandboxUnavailable = (
  over: Partial<StartSessionFailure> = {},
): StartSessionFailure => ({
  message: '提交失败：Docker sandbox unavailable: docker SDK not installed',
  status: 409,
  backend: 'docker',
  availableBackends: ['local'],
  ...over,
});

describe('TaskCreationDialog 409 切换动作（#1644）', () => {
  it('显式选 Docker 不可用：卡片给两条明示与「改用 local 重试」；点击以 sandbox_backend=local 重发原 payload', async () => {
    const failure = sandboxUnavailable();
    const onCreateTask = vi.fn(
      async (_p: StartSessionPayload): Promise<SubmitTaskResult> => null,
    );
    onCreateTask.mockResolvedValueOnce(failure).mockResolvedValueOnce(null);

    const onOpenChange = vi.fn();
    await act(async () => {
      render(dialog({ onCreateTask, onOpenChange }));
      await Promise.resolve();
    });

    // 走到会触发 409 的请求形状：运行位置显式选 Docker。
    const prompt = document.body.querySelector<HTMLTextAreaElement>('#task-prompt')!;
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(
        HTMLTextAreaElement.prototype,
        'value',
      )!.set!;
      setter.call(prompt, '给登录接口加上限流');
      prompt.dispatchEvent(new Event('input', { bubbles: true }));
      advancedToggle().click();
    });
    await act(async () => {
      document.body.querySelector<HTMLButtonElement>('[aria-label="运行位置"]')!.click();
    });
    const docker = [...document.body.querySelectorAll<HTMLElement>('[cmdk-item]')].find(
      (item) => item.querySelector('.picker-item-title')?.textContent?.trim() === 'Docker',
    );
    expect(docker).toBeTruthy();
    await act(async () => {
      docker!.click();
    });
    await act(async () => {
      [...document.body.querySelectorAll('button')]
        .find((b) => b.textContent === '创建并启动')!
        .click();
      await Promise.resolve();
    });

    expect(onCreateTask).toHaveBeenCalledTimes(1);
    const first = onCreateTask.mock.calls[0][0];
    expect(first.sandbox_backend).toBe('docker');

    const card = document.body.querySelector<HTMLElement>('.project-error')!;
    expect(card.textContent).toContain(failure.message);
    expect(card.textContent).toContain(LOCAL_BACKEND_NOTICE);
    expect(card.textContent).toContain(LOCAL_BACKEND_NTFS_NOTICE);
    const retry = [...card.querySelectorAll('button')].find(
      (b) => b.textContent === '改用 local 重试',
    );
    expect(retry).toBeTruthy();

    await act(async () => {
      retry!.click();
      await Promise.resolve();
    });
    expect(onCreateTask).toHaveBeenCalledTimes(2);
    const second = onCreateTask.mock.calls[1][0];
    // AC2：原 payload 重发，只强制运行位置 = local。
    expect(second.sandbox_backend).toBe('local');
    expect(second.task).toBe('给登录接口加上限流');
    expect(second.cwd).toBe('D:\\Projects\\Demo');
    // 第二次成功（resolve null）=> 请求关闭浮层。
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it('普通字符串失败：只渲染原因，不给切换按钮', async () => {
    await submitPrompt(async () => '提交失败：目录不存在：D:\\nope');

    const card = document.body.querySelector<HTMLElement>('.project-error')!;
    expect(card.textContent).toBe('提交失败：目录不存在：D:\\nope');
    expect(card.textContent).not.toContain(LOCAL_BACKEND_NOTICE);
    expect(
      [...card.querySelectorAll('button')].some((b) => b.textContent === '改用 local 重试'),
    ).toBe(false);
  });

  it('409 但 local 不在可用列表：不给切换按钮', async () => {
    const failure = sandboxUnavailable({ availableBackends: [] });
    await submitPrompt(async () => failure);

    const card = document.body.querySelector<HTMLElement>('.project-error')!;
    expect(card.textContent).toContain(failure.message);
    expect(card.textContent).not.toContain(LOCAL_BACKEND_NOTICE);
    expect(
      [...card.querySelectorAll('button')].some((b) => b.textContent === '改用 local 重试'),
    ).toBe(false);
  });

  it('500 且 backend=docker：不给切换按钮（状态码条件有牙）', async () => {
    const failure = sandboxUnavailable({ status: 500 });
    await submitPrompt(async () => failure);

    const card = document.body.querySelector<HTMLElement>('.project-error')!;
    expect(card.textContent).toContain(failure.message);
    expect(card.textContent).not.toContain(LOCAL_BACKEND_NOTICE);
    expect(
      [...card.querySelectorAll('button')].some((b) => b.textContent === '改用 local 重试'),
    ).toBe(false);
  });

  it('409 且 backend=local：不给切换按钮（backend 条件有牙）', async () => {
    const failure = sandboxUnavailable({ backend: 'local' });
    await submitPrompt(async () => failure);

    const card = document.body.querySelector<HTMLElement>('.project-error')!;
    expect(card.textContent).toContain(failure.message);
    expect(card.textContent).not.toContain(LOCAL_BACKEND_NOTICE);
    expect(
      [...card.querySelectorAll('button')].some((b) => b.textContent === '改用 local 重试'),
    ).toBe(false);
  });
});
