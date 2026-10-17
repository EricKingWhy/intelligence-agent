// @vitest-environment jsdom
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { SandboxBackendEntry } from '../lib/api';
import { TaskCreationDialog } from './TaskCreationDialog';
import type { Project } from '../types';

const project: Project = {
  id: 'p1',
  path: 'C:\\Projects\\Demo',
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

const globalBackends = [backend('local'), backend('docker'), backend('windows_psec')];

let container: HTMLDivElement | null = null;
let root: Root | null = null;
const originalScrollIntoView = HTMLElement.prototype.scrollIntoView;

function render(ui: ReactElement): void {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root!.render(ui));
}

const query = <T extends Element>(selector: string) => document.body.querySelector<T>(selector);

function openOptions(): string[] {
  // #1177 §B：运行位置已收进「高级设置」，默认折叠——先展开再点它的触发器。
  const advancedToggle = query<HTMLButtonElement>('.project-advanced-toggle');
  expect(advancedToggle).not.toBeNull();
  act(() => advancedToggle!.click());
  const trigger = query<HTMLButtonElement>('[aria-label="\u8fd0\u884c\u4f4d\u7f6e"]');
  expect(trigger).not.toBeNull();
  act(() => trigger!.click());
  return Array.from(document.body.querySelectorAll('[cmdk-item]'))
    .map((item) => item.textContent ?? '');
}

function dialog(backends: SandboxBackendEntry[]) {
  return (
    <TaskCreationDialog
      project={project}
      models={[]}
      sandboxBackends={backends}
      onOpenChange={() => {}}
      onCreateTask={async () => null}
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

describe('TaskCreationDialog sandbox options (#1071/#1048)', () => {
  it('shows available backends and never offers windows_psec', async () => {
    // #1048 AC2：windows_psec 降级为 legacy-only，探针结果里的它不再进下拉。
    await act(async () => {
      render(dialog(globalBackends));
      await Promise.resolve();
    });

    const options = openOptions();
    expect(options.some((title) => title.trim() === '\u672c\u673a')).toBe(true);
    expect(options.some((title) => title.includes('Docker'))).toBe(true);
    expect(options.some((title) => title.includes('Windows PSEC'))).toBe(false);
  });

  it('hides the 运行位置 field when the probe returns no backends', async () => {
    await act(async () => {
      render(dialog([]));
      await Promise.resolve();
    });

    // #1177 §B：运行位置在「高级设置」里，先展开——否则折叠态下"查无此项"是假阴性
    // （探针返回空与折叠隐藏会给出同一个 null，这条就测不出探针契约了）。
    const advancedToggle = query<HTMLButtonElement>('.project-advanced-toggle');
    expect(advancedToggle).not.toBeNull();
    act(() => advancedToggle!.click());
    expect(query('[aria-label="\u8fd0\u884c\u4f4d\u7f6e"]')).toBeNull();
  });
});
