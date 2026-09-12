/** 记忆列表 + 单条硬删（MEM-5 / #160）。
 *
 *  **不做第二套真相**（不变量 #22）：本 hook 只持有 `GET /api/memories` 的最近一次
 *  响应。`hiddenIds` 是唯一的本地投影，只在"删除请求往返期间"生效，两个结局都会
 *  重拉权威列表把它清掉（成功 → 该行由后端的缺席消失；失败 → 该行回来）。所以列表
 *  静止态永远等于最近一次后端响应——这正是 AC3「不得只做本地隐藏」的落地方式。
 *
 *  能力未装配（503）**不是加载失败**：`disabled` 与 `loadError` 是两条通道，
 *  面板据此显示"记忆未启用"而不是"加载失败 + 重试"（不变量 #21：可选能力故障
 *  不能拖垮核心，也不该让用户去重试一个配置状态）。 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { MemoryError, deleteMemory, isMemoryDisabled, listMemories } from '../lib/api';
import { MEMORY_PAGE_SIZE, hasMoreAfter, withoutIds } from '../lib/memory';
import type { MemorySummary } from '../types';

export interface MemoriesState {
  /** 最近一次权威响应（含在途删除中被暂时藏起来的行）。 */
  rows: MemorySummary[];
  /** 面板应渲染的行（`rows` 减去在途/已确认删除但重拉未落地的行）。 */
  visible: MemorySummary[];
  /** 首次加载中。 */
  loading: boolean;
  /** "加载更多"在途（与 `loading` 分开：首屏骨架不该在翻页时回来）。 */
  loadingMore: boolean;
  /** 非 503 的加载失败原因（**不清空**已加载的行——一次抖动不该把列表抹掉）。 */
  loadError: string | null;
  /** 记忆能力未装配（503）的后端原文；非空 = 降级态。 */
  disabled: string | null;
  hasMore: boolean;
  /** 删除在途的行 id（按钮渲染"删除中…"并禁用，防连点重复 DELETE）。 */
  pending: ReadonlySet<string>;
  /** 硬删一条：失败时**先把列表回滚成权威状态再抛**（调用方负责显示错误文案）。 */
  remove: (memoryId: string) => Promise<void>;
  loadMore: () => Promise<void>;
  /** 重新拉取（保留已加载的页数，不把列表缩回第一页）。 */
  retry: () => Promise<void>;
}

export function useMemories(): MemoriesState {
  const [rows, setRows] = useState<MemorySummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [disabled, setDisabled] = useState<string | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [pending, setPending] = useState<ReadonlySet<string>>(() => new Set());
  const [hiddenIds, setHiddenIds] = useState<ReadonlySet<string>>(() => new Set());

  // 请求世代：只有**最后发起**的那次允许落地。删除后的重拉与用户手动重试可能重叠，
  // 先发后到的旧响应不能覆盖新真相（同 useProjects 的 generation 纪律）。
  const generation = useRef(0);
  // 当前行数的镜像：`loadMore` 的 offset 与 `retry` 的 limit 都要"当下的条数"，
  // 而回调闭包里的 state 是发起时的快照。
  const rowsRef = useRef<MemorySummary[]>([]);

  /** 统一的错误落点：503 走降级通道，其余走错误通道。 */
  const recordError = useCallback((error: unknown) => {
    if (isMemoryDisabled(error)) {
      setDisabled(error instanceof Error ? error.message : '记忆未启用');
      setLoadError(null);
      return;
    }
    setLoadError((error as Error).message || '加载记忆失败');
  }, []);

  /** 拉第 0 页（`limit` 条）。保留已加载行数由调用方通过 `limit` 表达。 */
  const refetch = useCallback(
    async (limit: number) => {
      const gen = ++generation.current;
      try {
        const list = await listMemories(limit, 0);
        if (gen !== generation.current) return;
        rowsRef.current = list;
        setRows(list);
        setHasMore(hasMoreAfter(list.length, limit));
        setLoadError(null);
        setDisabled(null);
        // 已不在权威列表里的 id 从隐藏集合清掉（删除已落地）；仍在列表里的保留
        // ——它对应的 DELETE 还在途，现在取消隐藏会让该行闪回来。
        const present = new Set(list.map((row) => row.id));
        setHiddenIds((prev) => {
          const kept = [...prev].filter((id) => present.has(id));
          return kept.length === prev.size ? prev : new Set(kept);
        });
      } catch (error) {
        if (gen !== generation.current) return;
        recordError(error);
      } finally {
        // 无条件清首屏骨架：被更新的请求顶掉时也要清——否则面板会永远停在
        // "加载中"（新请求不会替上一次清，它只管自己的 finally）。
        setLoading(false);
      }
    },
    [recordError],
  );

  useEffect(() => {
    void refetch(MEMORY_PAGE_SIZE);
  }, [refetch]);

  const retry = useCallback(
    () => refetch(Math.max(rowsRef.current.length, MEMORY_PAGE_SIZE)),
    [refetch],
  );

  const loadMore = useCallback(async () => {
    if (loadingMore || !hasMore) return;
    const gen = ++generation.current;
    setLoadingMore(true);
    try {
      const page = await listMemories(MEMORY_PAGE_SIZE, rowsRef.current.length);
      if (gen !== generation.current) return;
      // 按 id 去重再追加：offset 分页在"翻页期间有行被删"时会让后端把边界行
      // 再给一次，直接 concat 会渲染出重复条目（本地去重只防重，不改顺序）。
      const seen = new Set(rowsRef.current.map((row) => row.id));
      const merged = [...rowsRef.current, ...page.filter((row) => !seen.has(row.id))];
      rowsRef.current = merged;
      setRows(merged);
      setHasMore(hasMoreAfter(page.length, MEMORY_PAGE_SIZE));
      setLoadError(null);
    } catch (error) {
      if (gen !== generation.current) return;
      recordError(error);
    } finally {
      // 同样无条件复位：被重拉顶掉时若不清，按钮会永远停在"加载中"且再也点不动。
      setLoadingMore(false);
    }
  }, [hasMore, loadingMore, recordError]);

  const remove = useCallback(
    async (memoryId: string) => {
      if (pending.has(memoryId)) return;
      // 立即藏起来（AC3「删除后列表立即反映」）；请求收口前不改变任何后端事实。
      setHiddenIds((prev) => new Set(prev).add(memoryId));
      setPending((prev) => new Set(prev).add(memoryId));
      let deleted = false;
      try {
        const result = await deleteMemory(memoryId);
        // 200 但 `deleted` 不为 true = 后端没确认删掉：不能凭状态码就假装它没了。
        if (!result.deleted) throw new MemoryError(200, '后端未确认删除，该条记忆仍在。');
        deleted = true;
      } catch (error) {
        // 失败 = 回滚：取消隐藏 + 重拉权威列表，然后把错误抛给调用方显示。
        setHiddenIds((prev) => {
          const next = new Set(prev);
          next.delete(memoryId);
          return next;
        });
        await refetch(Math.max(rowsRef.current.length, MEMORY_PAGE_SIZE));
        throw error;
      } finally {
        setPending((prev) => {
          const next = new Set(prev);
          next.delete(memoryId);
          return next;
        });
      }
      // 成功：重拉权威列表。重拉失败也不取消隐藏——200 已经证明这条不存在了，
      // 再把它显示回来才是伪造；loadError 会提示列表可能不是最新（可重试）。
      if (deleted) await refetch(Math.max(rowsRef.current.length, MEMORY_PAGE_SIZE));
    },
    [pending, refetch],
  );

  const visible = useMemo(() => withoutIds(rows, hiddenIds), [rows, hiddenIds]);

  return {
    rows,
    visible,
    loading,
    loadingMore,
    loadError,
    disabled,
    hasMore,
    pending,
    remove,
    loadMore,
    retry,
  };
}
