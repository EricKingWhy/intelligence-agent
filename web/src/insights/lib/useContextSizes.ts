/** 「上下文大小」排序的懒加载数据源（#1420）。
 *
 *  后端 overview 行没有窗口占用字段；票面允许复用既有
 *  `GET /api/sessions/{id}/context-usage`。只有用户**真的**选了该排序才发请求，
 *  并发上限 4，失败 / `no_data` 记为 null（排最后，不拿别的字段冒充）。
 *  不缓存到全局：切回「最近 / 用量」后不再请求，重选时只补缺失项。 */

import { useEffect, useRef, useState } from 'react';
import { getContextUsage } from '../../lib/api';

export function useContextSizes(
  sessionIds: readonly string[],
  enabled: boolean,
): { sizes: Record<string, number | null>; loading: boolean } {
  const [sizes, setSizes] = useState<Record<string, number | null>>({});
  const [loading, setLoading] = useState(false);
  const sizesRef = useRef(sizes);
  useEffect(() => {
    sizesRef.current = sizes;
  }, [sizes]);
  const key = sessionIds.join('\u0000');
  useEffect(() => {
    if (!enabled) return undefined;
    const ids = key === '' ? [] : key.split('\u0000');
    const missing = ids.filter((id) => !(id in sizesRef.current));
    if (missing.length === 0) return undefined;
    let cancelled = false;
    setLoading(true);
    const queue = [...missing];
    const worker = async () => {
      for (;;) {
        const id = queue.shift();
        if (id === undefined || cancelled) return;
        try {
          const usage = await getContextUsage(id);
          if (cancelled) return;
          setSizes((prev) => ({
            ...prev,
            [id]: usage.state === 'no_data' ? null : usage.used_tokens,
          }));
        } catch {
          if (cancelled) return;
          setSizes((prev) => ({ ...prev, [id]: null }));
        }
      }
    };
    void Promise.all(Array.from({ length: Math.min(4, missing.length) }, worker))
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, key]);
  return { sizes, loading };
}
