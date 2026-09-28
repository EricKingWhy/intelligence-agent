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
