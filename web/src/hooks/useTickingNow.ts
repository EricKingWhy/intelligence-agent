/** useTickingNow — 每分钟推进一次的 `now` 时间戳，驱动相对时间自动刷新。
 *
 * 背景：formatRelativeTime 在渲染时计算，组件不重渲染就一直显示旧值（issue #40）。
 * 这个 hook 暴露一个会自己更新的 `now`，把它喂给 formatRelativeTime 即可让相对
 * 时间随真实时间流逝刷新——而不是停在挂载瞬间的快照。
 *
 * 心跳频率 60s 与 formatRelativeTime 的最小语义粒度（分钟）对齐；更高频率纯属浪费。
 * 组件卸载即清 interval，不会泄漏。 */
import { useEffect, useState } from 'react';

const HEARTBEAT_MS = 60_000;

export function useTickingNow(): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), HEARTBEAT_MS);
    return () => clearInterval(id);
  }, []);
  return now;
}
