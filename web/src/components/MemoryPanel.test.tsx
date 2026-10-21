// @vitest-environment jsdom
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useMemories, type MemoriesState } from '../hooks/useMemories';
import { MemoryPanel } from './MemoryPanel';
import type { MemoryRecord } from '../lib/memoryV2Api';

vi.mock('../hooks/useMemories', () => ({ useMemories: vi.fn() }));

let host: HTMLDivElement;
let root: Root;

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
  vi.mocked(useMemories).mockReset();
  vi.unstubAllGlobals();
});

function memoriesState(overrides: Partial<MemoriesState> = {}): MemoriesState {
  return {
    visible: [],
    loading: false,
    loadingMore: false,
    loadError: null,
    disabled: null,
    hasMore: false,
    paginationLimitReached: false,
    pending: new Set(),
    remove: vi.fn(async () => {}),
    loadMore: vi.fn(async () => {}),
    retry: vi.fn(async () => {}),
    retryFailedPage: vi.fn(async () => {}),
    ...overrides,
  };
}

async function renderPanel(state: MemoriesState): Promise<void> {
  vi.mocked(useMemories).mockReturnValue(state);
  vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
    const url = String(input);
    return Promise.resolve(response(url.includes('/api/memory-settings')
      ? { extraction_enabled: true, recall_enabled: true, fallback_enabled: true }
      : []));
  }));
  await act(async () => {
    root.render(<MemoryPanel open onOpenChange={() => {}} />);
    await Promise.resolve();
  });
}

describe('MemoryPanel list states', () => {
  it('shows loading without presenting an empty state', async () => {
    await renderPanel(memoriesState({ loading: true }));

    expect(document.body.querySelector('.memory-loading')?.textContent).toContain('正在加载记忆');
    expect(document.body.querySelector('.memory-empty')).toBeNull();
  });

  it('shows an empty state only after a successful empty response', async () => {
    await renderPanel(memoriesState());

    expect(document.body.querySelector('.memory-empty')?.textContent).toContain('还没有记忆');
    expect(document.body.querySelector('.memory-degraded')).toBeNull();
    expect(document.body.querySelector('.memory-error')).toBeNull();
  });

  it('shows degraded capability as a neutral empty state (no red failure, no retry)', async () => {
    await renderPanel(memoriesState({ disabled: 'memory capability 未启用' }));

    // #1372 改动 9：未启用 = 配置缺省，不是故障——中性空态 + 去设置入口。
    const empty = document.body.querySelector('.memory-empty.memory-disabled');
    expect(empty?.textContent).toContain('记忆管理未启用');
    expect(document.body.querySelector('.memory-error')).toBeNull();
    expect(document.body.querySelector('.memory-degraded')).toBeNull();
    const action = empty?.querySelector('button');
    expect(action?.textContent).toBe('查看记忆设置');
    expect(empty?.textContent).not.toContain('重试');
  });

  it('keeps retryable read errors visible and retries the failed page', async () => {
    const retryFailedPage = vi.fn(async () => {});
    await renderPanel(memoriesState({ loadError: 'temporary failure', retryFailedPage }));

    const error = document.body.querySelector('.memory-error');
    expect(error?.textContent).toContain('temporary failure');
    expect(document.body.querySelector('.memory-empty')).toBeNull();

    const retry = error?.querySelector('button');
    expect(retry?.textContent).toBe('重试');
    await act(async () => { retry?.click(); });
    expect(retryFailedPage).toHaveBeenCalledOnce();
  });
});

describe('MemoryPanel pagination boundary', () => {
  it('shows the query ceiling instead of claiming that every record was loaded', async () => {
    const record: MemoryRecord = {
      id: 'm-boundary',
      content: 'boundary record',
      is_tombstone: false,
      deleted_at: null,
      scope: 'user_global',
      metadata: {},
      created_at: '2026-09-25T10:00:00Z',
      root_id: 'm-boundary',
      version: 1,
      kind: 'semantic',
      tier: 'profile',
      status: 'active',
      project_id: null,
      source_type: 'automatic',
      source_session_id: null,
      source_event_ids: [],
      payload: { kind: 'semantic', subject: 'user', fact: 'boundary', category: 'profile' },
    };
    const state: MemoriesState = {
      visible: [record],
      loading: false,
      loadingMore: false,
      loadError: null,
      disabled: null,
      hasMore: false,
      paginationLimitReached: true,
      pending: new Set(),
      remove: vi.fn(async () => {}),
      loadMore: vi.fn(async () => {}),
      retry: vi.fn(async () => {}),
      retryFailedPage: vi.fn(async () => {}),
    };
    vi.mocked(useMemories).mockReturnValue(state);
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(url.includes('/api/memory-settings')
        ? response({ extraction_enabled: true, recall_enabled: true, fallback_enabled: true })
        : response([]));
    }));

    await act(async () => {
      root.render(<MemoryPanel open onOpenChange={() => {}} />);
      await Promise.resolve();
    });

    const footer = document.body.querySelector('.memory-more-end');
    expect(footer?.getAttribute('role')).toBe('status');
    expect(footer?.textContent).toContain('最多 10,050 条');
    expect(footer?.textContent).toContain('已达到查询上限');
    expect(footer?.textContent).toContain('缩小筛选范围');
    expect(document.body.querySelector('.memory-more-btn')).toBeNull();
  });
});

describe('MemoryPanel fallback switch', () => {
  it('renders the fallback switch from the server and persists a toggle', async () => {
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.includes('/api/memory-settings') && init?.method === 'PATCH') {
        return Promise.resolve(response({
          extraction_enabled: true, recall_enabled: true, fallback_enabled: true,
        }));
      }
      if (url.includes('/api/memory-settings')) {
        return Promise.resolve(response({
          extraction_enabled: true, recall_enabled: true, fallback_enabled: false,
        }));
      }
      return Promise.resolve(response([]));
    });
    vi.mocked(useMemories).mockReturnValue(memoriesState());
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => {
      root.render(<MemoryPanel open onOpenChange={() => {}} />);
      await Promise.resolve();
    });

    const label = Array.from(document.body.querySelectorAll('label'))
      .find((item) => item.textContent?.includes('确定性错误时允许降级到备用模型'));
    const checkbox = label?.querySelector('input[type="checkbox"]') as HTMLInputElement | null;
    expect(checkbox).not.toBeNull();
    expect(checkbox?.checked).toBe(false);

    await act(async () => { checkbox?.click(); await Promise.resolve(); });

    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'PATCH');
    expect(patchCall).toBeDefined();
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({ fallback_enabled: true });
  });
});
