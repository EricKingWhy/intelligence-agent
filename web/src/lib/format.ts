/** 展示层格式化工具——跨组件共享的时间/数值形状。 */

/** duration 格式化：<1s 用 ms，否则一位小数秒。无完成时间（running 中）返回 null。 */
export function formatDuration(startedAt?: string, completedAt?: string): string | null {
  if (!startedAt || !completedAt) return null;
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (ms < 1000) return `${Math.max(1, ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
