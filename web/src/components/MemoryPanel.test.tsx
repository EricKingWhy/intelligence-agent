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
    };
    vi.mocked(useMemories).mockReturnValue(state);
    vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
      const url = String(input);
      return Promise.resolve(url.includes('/api/memory-settings')
        ? response({ extraction_enabled: true, recall_enabled: true })
        : response([]));
    }));

    await act(async () => {
      root.render(<MemoryPanel open onOpenChange={() => {}} />);
      await Promise.resolve();
    });

    const footer = document.body.querySelector('.memory-more-end');
    expect(footer?.textContent).toContain('最多 10,050 条');
    expect(footer?.textContent).toContain('缩小筛选范围');
    expect(footer?.textContent).not.toBe('已全部加载');
    expect(document.body.querySelector('.memory-more-btn')).toBeNull();
  });
});
