// @vitest-environment jsdom
import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useMemories, type MemoriesState } from './useMemories';

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function memory(id: string) {
  return { id, content: id, scope: 'user', metadata: {}, created_at: '2026-09-25T10:00:00Z' };
}

let host: HTMLDivElement;
let root: Root;
let current: MemoriesState | undefined;

function Harness({ query }: { query: string }) {
  const state = useMemories({ q: query });
  useEffect(() => { current = state; }, [state]);
  return null;
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
  current = undefined;
  vi.unstubAllGlobals();
});

describe('useMemories filter changes during mutations', () => {
  it.each(['success', 'failure'] as const)(
    'does not let a pending delete %s refetch overwrite the newly selected filter',
    async (outcome) => {
      const initialOldList = deferred<Response>();
      const newList = deferred<Response>();
      const deletion = deferred<Response>();
      let oldListCalls = 0;
      const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
        const url = new URL(String(input), 'http://localhost');
        if (init?.method === 'DELETE') return deletion.promise;
        if (url.searchParams.get('q') === 'old') {
          oldListCalls += 1;
          return oldListCalls === 1
            ? initialOldList.promise
            : Promise.resolve(response([memory('stale-old-filter-result')]));
        }
        return newList.promise;
      });
      vi.stubGlobal('fetch', fetchMock);

      await act(async () => { root.render(<Harness query="old" />); });
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      await act(async () => { initialOldList.resolve(response([memory('m-old')])); });
      expect(current?.visible.map(({ id }) => id)).toEqual(['m-old']);

      let remove!: Promise<void>;
      act(() => { remove = current!.remove('m-old'); });
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

      await act(async () => { root.render(<Harness query="new" />); });
      await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
      await act(async () => { newList.resolve(response([memory('m-new')])); });
      expect(current?.visible.map(({ id }) => id)).toEqual(['m-new']);

      await act(async () => {
        deletion.resolve(outcome === 'success'
          ? response({ id: 'm-old', deleted: true })
          : response({ detail: 'delete failed' }, 500));
        if (outcome === 'success') await remove;
        else await expect(remove).rejects.toThrow();
      });

      expect(current?.loading).toBe(false);
      expect(current?.visible.map(({ id }) => id)).toEqual(['m-new']);
      expect(current?.pending.has('m-old')).toBe(false);
      expect(fetchMock).toHaveBeenCalledTimes(3);
    },
  );
});

describe('useMemories pagination retry', () => {
  it('retries the failed next-page request at the same offset', async () => {
    const firstPage = Array.from({ length: 50 }, (_, index) => memory(`m-${index}`));
    const secondPage = Array.from({ length: 5 }, (_, index) => memory(`m-${index + 50}`));
    const requestedOffsets: string[] = [];
    let nextPageAttempts = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      const offset = url.searchParams.get('offset') ?? '0';
      requestedOffsets.push(offset);
      if (offset === '50') {
        nextPageAttempts += 1;
        return Promise.resolve(nextPageAttempts === 1
          ? response({ detail: 'temporary page failure' }, 503)
          : response(secondPage));
      }
      return Promise.resolve(response(firstPage));
    });
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => { root.render(<Harness query="" />); });
    await vi.waitFor(() => expect(current?.visible).toHaveLength(50));

    await act(async () => { await current!.loadMore(); });
    expect(current?.loadError).toContain('temporary page failure');

    await act(async () => { await current!.retryFailedPage(); });

    expect(requestedOffsets.filter((offset) => offset === '50')).toHaveLength(2);
    expect(current?.visible).toHaveLength(55);
    expect(current?.loadError).toBeNull();
  });

  it('performs a full first-page refresh after a later-page failure', async () => {
    const firstPage = Array.from({ length: 50 }, (_, index) => memory(`m-${index}`));
    const refreshedPage = Array.from({ length: 50 }, (_, index) => memory(`m-${index + 1}`));
    const requestedOffsets: string[] = [];
    let firstPageCalls = 0;
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      const offset = url.searchParams.get('offset') ?? '0';
      requestedOffsets.push(offset);
      if (offset === '50') {
        return Promise.resolve(response({ detail: 'temporary page failure' }, 503));
      }
      firstPageCalls += 1;
      return Promise.resolve(response(firstPageCalls === 1 ? firstPage : refreshedPage));
    });
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => { root.render(<Harness query="" />); });
    await vi.waitFor(() => expect(current?.visible).toHaveLength(50));
    await act(async () => { await current!.loadMore(); });
    expect(current?.loadError).toContain('temporary page failure');

    // Mutation completion and the manual Refresh button both need this full refresh path.
    await act(async () => { await current!.retry(); });

    expect(requestedOffsets).toEqual(['0', '50', '0']);
    expect(current?.visible.map(({ id }) => id)).not.toContain('m-0');
    expect(current?.visible.map(({ id }) => id)).toContain('m-50');
    expect(current?.loadError).toBeNull();
  });

  it('advances the server offset even when a later page repeats a visible record', async () => {
    const firstPage = Array.from({ length: 50 }, (_, index) => memory(`m-${index}`));
    const secondPage = [memory('m-49'), ...Array.from({ length: 49 }, (_, index) => memory(`m-${index + 50}`))];
    const requestedOffsets: string[] = [];
    const fetchMock = vi.fn((input: RequestInfo | URL) => {
      const url = new URL(String(input), 'http://localhost');
      const offset = url.searchParams.get('offset') ?? '0';
      requestedOffsets.push(offset);
      if (offset === '50') return Promise.resolve(response(secondPage));
      if (offset === '100') return Promise.resolve(response([memory('m-99')]));
      return Promise.resolve(response(firstPage));
    });
    vi.stubGlobal('fetch', fetchMock);

    await act(async () => { root.render(<Harness query="" />); });
    await vi.waitFor(() => expect(current?.visible).toHaveLength(50));
    await act(async () => { await current!.loadMore(); });
    expect(current?.visible).toHaveLength(99);

    await act(async () => { await current!.loadMore(); });

    expect(requestedOffsets).toContain('100');
    expect(current?.visible).toHaveLength(100);
  });
});
