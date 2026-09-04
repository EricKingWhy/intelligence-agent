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

/** 不可信大输出的展示截断上限（字符）。20k 字符远超正常阅读需要，
 *  又足以拦住 MB 级 stdout / JSON 把主线程卡死的前端 DoS（安全审查发现 1）。 */
export const DISPLAY_TRUNCATE_LIMIT = 20_000;

/** 渲染层防御：失控模型/工具可返回 MB 级输出，全量进 <pre> 会冻结 UI。
 *  只截显示——投影层与 state 保持全量真相（不变量 #22），Inspector 钻取
 *  数据不受影响。超限时附截断标记（含原始长度，零伪造）。 */
export function truncateForDisplay(text: string, max: number = DISPLAY_TRUNCATE_LIMIT): string {
  if (text.length <= max) return text;
  return `${text.slice(0, max)}\n…（已截断，完整长度 ${text.length.toLocaleString()} 字符）`;
}
