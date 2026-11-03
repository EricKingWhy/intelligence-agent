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

interface OptionText {
  title: string;
  description: string;
}

// #1486：标题与描述分开取。`[cmdk-item]` 的 textContent 会把 title 与
// description 拼在一起（OptionPicker 的 OptionRowContent），拿它做等值比较
// 会在选项补了描述之后假红。
function openOptions(): OptionText[] {
  // #1177 §B：运行位置已收进「高级设置」，默认折叠——先展开再点它的触发器。
  const advancedToggle = query<HTMLButtonElement>('.project-advanced-toggle');
  expect(advancedToggle).not.toBeNull();
  act(() => advancedToggle!.click());
  const trigger = query<HTMLButtonElement>('[aria-label="\u8fd0\u884c\u4f4d\u7f6e"]');
  expect(trigger).not.toBeNull();
  act(() => trigger!.click());
  return Array.from(document.body.querySelectorAll('[cmdk-item]')).map((item) => ({
    title: item.querySelector('.picker-item-title')?.textContent?.trim() ?? '',
    description: item.querySelector('.picker-item-desc')?.textContent?.trim() ?? '',
  }));
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
    const titles = options.map((option) => option.title);
    // 比的是 title，不是整个 item 的文本：描述里也会出现这些字。
    expect(titles).toContain('\u672c\u673a');
    expect(titles).toContain('Docker');
    expect(titles.some((title) => title.includes('Windows PSEC'))).toBe(false);

    // #1486 AC3：local 无 OS 级隔离必须在选项里明说（与 CLI
    // LOCAL_SANDBOX_NOTICE 同义）；docker 档的描述必须对得上实现的三项降权。
    const local = options.find((option) => option.title === '\u672c\u673a');
    expect(local?.description).toContain('无 OS 级');
    expect(local?.description).toContain('文件');
    expect(local?.description).toContain('网络');

    const dockerOption = options.find((option) => option.title === 'Docker');
    expect(dockerOption?.description).toContain('非 root');
    expect(dockerOption?.description).toContain('cap_drop=ALL');
    expect(dockerOption?.description).toContain('no-new-privileges');
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
