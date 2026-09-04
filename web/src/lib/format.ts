/** 展示层格式化工具——跨组件共享的时间/数值形状。 */

/** duration 格式化：<1s 用 ms，否则一位小数秒。无完成时间（running 中）返回 null。 */
export function formatDuration(startedAt?: string, completedAt?: string): string | null {
  if (!startedAt || !completedAt) return null;
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (ms < 1000) return `${Math.max(1, ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** 相对时间：把 ISO 时间戳渲染成 '刚刚 / N 分钟前 / N 小时前 / N 天前 / M月D日'。
 *  now 参数化以避免 Date.now() 副作用（可测、可由 tick hook 注入最新值）。
 *  缺省 now 用 Date.now()，保持调用点零改动。 */
export function formatRelativeTime(iso: string | null, now: number = Date.now()): string {
  if (!iso) return '';
  const diffMs = now - new Date(iso).getTime();
  const minutes = Math.floor(diffMs / 60000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  return new Date(iso).toLocaleDateString([], { month: 'short', day: 'numeric' });
}
