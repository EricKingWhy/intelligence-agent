// @vitest-environment jsdom
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, getSandboxBackends: vi.fn() };
});

import { getSandboxBackends, type SandboxBackendEntry } from '../lib/api';
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
  reason: null,
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
  const trigger = query<HTMLButtonElement>('[aria-label="\u8fd0\u884c\u4f4d\u7f6e"]');
  expect(trigger).not.toBeNull();
  act(() => trigger!.click());
  return Array.from(document.body.querySelectorAll('[cmdk-item]'))
    .map((item) => item.textContent ?? '');
}

function dialog() {
  return (
    <TaskCreationDialog
      project={project}
      models={[]}
      sandboxBackends={globalBackends}
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
  vi.mocked(getSandboxBackends).mockReset();
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

describe('TaskCreationDialog sandbox options (#1071)', () => {
  it('keeps generic choices when the workspace probe returns no entries', async () => {
    vi.mocked(getSandboxBackends).mockResolvedValue([]);

    await act(async () => {
      render(dialog());
      await Promise.resolve();
    });

    const options = openOptions();
    expect(options.some((title) => title.trim() === '\u672c\u673a')).toBe(true);
    expect(options.some((title) => title.includes('Docker'))).toBe(true);
    expect(options.some((title) => title.includes('Windows PSEC'))).toBe(false);
  });

  it('keeps generic choices when the workspace probe rejects', async () => {
    vi.mocked(getSandboxBackends).mockRejectedValue(new Error('probe failed'));

    await act(async () => {
      render(dialog());
      await Promise.resolve();
    });

    const options = openOptions();
    expect(options.some((title) => title.trim() === '\u672c\u673a')).toBe(true);
    expect(options.some((title) => title.includes('Docker'))).toBe(true);
    expect(options.some((title) => title.includes('Windows PSEC'))).toBe(false);
  });

  it('does not offer PSEC while the workspace-specific probe is pending', async () => {
    let resolveProbe!: (entries: SandboxBackendEntry[]) => void;
    const probe = new Promise<SandboxBackendEntry[]>((resolve) => {
      resolveProbe = resolve;
    });
    vi.mocked(getSandboxBackends).mockReturnValue(probe);

    render(dialog());

    const options = openOptions();
    expect(options.some((title) => title.trim() === '\u672c\u673a')).toBe(true);
    expect(options.some((title) => title.trim() === 'Docker')).toBe(true);
    expect(options.some((title) => title.includes('Windows PSEC'))).toBe(false);

    await act(async () => {
      resolveProbe([]);
      await probe;
    });
  });

  it('never offers PSEC even from a successful workspace-specific probe', async () => {
    // #1048 AC2：windows_psec 降级为 legacy-only，任何来源的探针都不再进下拉。
    vi.mocked(getSandboxBackends).mockResolvedValue([
      backend('windows_psec'),
      backend('docker'),
    ]);

    await act(async () => {
      render(dialog());
      await Promise.resolve();
    });

    const options = openOptions();
    expect(options.some((title) => title.includes('Windows PSEC'))).toBe(false);
    expect(options.some((title) => title.includes('Docker'))).toBe(true);
  });
});
