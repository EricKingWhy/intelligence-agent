/** Recent authoritative memory list state for the management panel. */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MemoryError, describeMemoryError, isMemoryDisabled } from '../lib/api';
import {
  deleteMemoryRecord,
  listMemoryRecords,
  type MemoryFilters,
  type MemoryRecord,
} from '../lib/memoryV2Api';
import { MEMORY_PAGE_SIZE, hasMoreAfter, refetchLimit } from '../lib/memory';
import { withTimeout } from '../lib/timeout';

interface PageState {
  key: string;
  rows: MemoryRecord[];
  hiddenIds: ReadonlySet<string>;
  status: 'loading' | 'ready' | 'error' | 'disabled';
  loadingMore: boolean;
  hasMore: boolean;
  loadError: string | null;
  disabled: string | null;
}

export interface MemoriesState {
  visible: MemoryRecord[];
  loading: boolean;
  loadingMore: boolean;
  loadError: string | null;
  disabled: string | null;
  hasMore: boolean;
  pending: ReadonlySet<string>;
  remove: (memoryId: string, projectId?: string) => Promise<void>;
  loadMore: () => Promise<void>;
  retry: () => Promise<void>;
}

const DELETE_TIMEOUT_MS = 30_000;
const EMPTY_FILTERS: MemoryFilters = {};
const EMPTY_ROWS: MemoryRecord[] = [];

export function useMemories(filters: MemoryFilters = EMPTY_FILTERS): MemoriesState {
  const query = filters.q?.trim() ?? '';
  const kind = filters.kind;
  const status = filters.status;
  const scope = filters.scope;
  const projectId = filters.project_id;
  const filterKey = JSON.stringify([query, kind ?? '', status ?? '', scope ?? '', projectId ?? '']);
  const apiFilters = useMemo<MemoryFilters>(() => ({
    ...(query ? { q: query } : {}),
    ...(kind ? { kind } : {}),
    ...(status ? { status } : {}),
    ...(scope ? { scope } : {}),
    ...(projectId ? { project_id: projectId } : {}),
  }), [query, kind, status, scope, projectId]);

  const [page, setPage] = useState<PageState>({
    key: '',
    rows: [],
    hiddenIds: new Set(),
    status: 'loading',
    loadingMore: false,
    hasMore: false,
    loadError: null,
    disabled: null,
  });
  const [pending, setPending] = useState<ReadonlySet<string>>(() => new Set());
  const generation = useRef(0);

  const refetch = useCallback(async (limit: number) => {
    const gen = ++generation.current;
    try {
      const rows = await listMemoryRecords(limit, 0, apiFilters);
      if (gen !== generation.current) return;
      setPage((previous) => {
        const hiddenIds = previous.key === filterKey
          ? new Set([...previous.hiddenIds].filter((id) => rows.some((row) => row.id === id)))
          : new Set<string>();
        return {
          key: filterKey,
          rows,
          hiddenIds,
          status: 'ready',
          loadingMore: false,
          hasMore: hasMoreAfter(rows.length, limit),
          loadError: null,
          disabled: null,
        };
      });
    } catch (error) {
      if (gen !== generation.current) return;
      const message = describeMemoryError(error, '加载记忆失败');
      setPage((previous) => ({
        key: filterKey,
        rows: previous.key === filterKey ? previous.rows : [],
        hiddenIds: previous.key === filterKey ? previous.hiddenIds : new Set<string>(),
        status: isMemoryDisabled(error) ? 'disabled' : 'error',
        loadingMore: false,
        hasMore: previous.key === filterKey && previous.hasMore,
        loadError: isMemoryDisabled(error) ? null : message,
        disabled: isMemoryDisabled(error) ? message : null,
      }));
    }
  }, [apiFilters, filterKey]);

  // Start the API request here; state changes happen only after the request settles.
  useEffect(() => {
    // The async request writes state only after its response arrives.
    // oxlint-disable-next-line react/set-state-in-effect
    void refetch(MEMORY_PAGE_SIZE);
  }, [refetch]);

  const currentRows = page.key === filterKey ? page.rows : EMPTY_ROWS;
  const visible = useMemo(
    () => currentRows.filter((row) => !page.hiddenIds.has(row.id)),
    [currentRows, page.hiddenIds],
  );

  const retry = useCallback(
    async () => {
      setPage((previous) => previous.key === filterKey
        ? { ...previous, status: 'loading', loadError: null, disabled: null }
        : previous);
      await refetch(refetchLimit(currentRows.length));
    }, [currentRows.length, filterKey, refetch],
  );

  const loadMore = useCallback(async () => {
    if (page.key !== filterKey || page.loadingMore || !page.hasMore) return;
    const gen = ++generation.current;
    setPage((previous) => previous.key === filterKey
      ? { ...previous, loadingMore: true }
      : previous);
    try {
      const nextPage = await listMemoryRecords(MEMORY_PAGE_SIZE, page.rows.length, apiFilters);
      if (gen !== generation.current) return;
      setPage((previous) => {
        if (previous.key !== filterKey) return previous;
        const seen = new Set(previous.rows.map((row) => row.id));
        const rows = [...previous.rows, ...nextPage.filter((row) => !seen.has(row.id))];
        return {
          ...previous,
          rows,
          loadingMore: false,
          hasMore: hasMoreAfter(nextPage.length, MEMORY_PAGE_SIZE),
          loadError: null,
        };
      });
    } catch (error) {
      if (gen !== generation.current) return;
      setPage((previous) => previous.key === filterKey
        ? {
          ...previous,
          loadingMore: false,
          loadError: describeMemoryError(error, '加载更多记忆失败'),
        }
        : previous);
    }
  }, [apiFilters, filterKey, page]);

  const remove = useCallback(async (memoryId: string, projectId?: string) => {
    if (pending.has(memoryId)) return;
    const retainedLimit = refetchLimit(currentRows.length);
    setPending((previous) => new Set(previous).add(memoryId));
    let deleted = false;
    try {
      deleted = await withTimeout(
        deleteMemoryRecord(memoryId, projectId), DELETE_TIMEOUT_MS, '删除请求',
      );
      if (!deleted) throw new MemoryError(200, '后端未确认删除，该条记忆仍在。');
    } catch (error) {
      await refetch(retainedLimit);
      setPending((previous) => {
        const next = new Set(previous);
        next.delete(memoryId);
        return next;
      });
      throw error;
    }

    setPage((previous) => previous.key === filterKey
      ? { ...previous, hiddenIds: new Set(previous.hiddenIds).add(memoryId) }
      : previous);
    await refetch(retainedLimit);
    setPending((previous) => {
      const next = new Set(previous);
      next.delete(memoryId);
      return next;
    });
  }, [currentRows.length, filterKey, pending, refetch]);

  const matches = page.key === filterKey;
  return {
    visible,
    loading: !matches || page.status === 'loading',
    loadingMore: matches && page.loadingMore,
    loadError: matches ? page.loadError : null,
    disabled: matches ? page.disabled : null,
    hasMore: matches && page.hasMore,
    pending,
    remove,
    loadMore,
    retry,
  };
}
