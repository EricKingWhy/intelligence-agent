/** 展示层格式化工具——跨组件共享的时间/数值形状。 */

/** 短时长语义（UI-04 信任裂缝）：<50ms 显 '<50ms'——测不到的就说测不到，
 *  不再造「1ms」这类假精度（mock/真实快事件都会撞上）；<1s 显整 ms；
 *  否则一位小数秒。非有限 / 负值（时钟倒挂）同样不显数字，返回空串交给调用方隐藏。 */
export function formatShortDuration(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 50) return '<50ms';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

/** duration 格式化：走 formatShortDuration 同一语义（时钟倒挂返回 null，
 *  调用方不渲染该行——与 ReasoningBlock 的 ms<0 守卫同一口径）。无完成时间（running 中）返回 null。 */
export function formatDuration(startedAt?: string, completedAt?: string): string | null {
  if (!startedAt || !completedAt) return null;
  const ms = new Date(completedAt).getTime() - new Date(startedAt).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  return formatShortDuration(ms);
}

/** 粗粒度时长（委派 / 子智能体 roster 这类**分钟级**场景，可读性优先）：
 *  <60s → 'N秒'；<60min → 'M分S秒'；否则 'H小时M分'。
 *
 *  与 formatShortDuration 的分工：后者是**工具调用**级精度（<50ms/ms/X.Xs），
 *  委派动辄几分钟，用它会把 12 分钟显示成 '738.2s'。两种语言各服务各的量级，
 *  不是同一语义的两种写法。
 *
 *  非有限 / 负值（时钟倒挂）→ null（不显数字，调用方隐藏该行，与 formatDuration 同口径）。 */
export function formatCoarseDuration(ms: number): string | null {
  if (!Number.isFinite(ms) || ms < 0) return null;
  const totalSec = Math.floor(ms / 1000);
  if (totalSec < 60) return `${totalSec}秒`;
  const minutes = Math.floor(totalSec / 60);
  const seconds = totalSec % 60;
  if (minutes < 60) return `${minutes}分${seconds}秒`;
  const hours = Math.floor(minutes / 60);
  return `${hours}小时${minutes % 60}分`;
}

/** 全时间戳（本地时区，含毫秒）：'YYYY-MM-DD HH:mm:ss.fff'。
 *  Inspector 浮层 / Tooltip 等需要完整精度的展示场景使用（相对时间不够看时）。
 *  非法或缺失时间返回 null——调用方据此决定是否渲染该行。 */
export function formatTimestamp(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const p = (n: number, w = 2) => String(n).padStart(w, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

/** 面向用户的日期时间（本地时区，到分钟）：'YYYY-MM-DD HH:mm'（#1372 改动 10）。
 *
 *  为什么单独一个函数而不是复用 `formatTimestamp`：后者带秒与毫秒，是事件级证据的
 *  精度；面向用户的状态行（开始时间 / 完成时间 / 记忆时间 / 最近检查）只需要到分钟。
 *  两种精度都是同一套「YYYY-MM-DD HH:mm」前缀，不再出现 `toLocaleString()` 的
 *  `9/7/2026, 12:00:00 AM` 这类随环境漂移的格式。
 *  非法或缺失时间返回 null——调用方决定隐藏该行。 */
export function formatDateTime(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

/** 相对时间：把 ISO 时间戳渲染成 '刚刚 / N 分钟前 / N 小时前 / N 天前 / YYYY-MM-DD HH:mm'。
 *  超过一周落到 `formatDateTime` 的绝对格式（不再用 locale 的「9月7日」）。
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
  // #1372 改动 10：超过一周不再落回 locale 的「9月7日」，统一全站日期时间格式。
  return formatDateTime(iso) ?? '';
}

/** 字节数人性化——**唯一实现**（#937 / M-22 收口：此前 format.ts / attachments.ts /
 *  StepDetail.tsx 三处并存，同一输入渲染出 '2.0 MB' 与 '2 MiB' 两种串）。
 *  1024 进制 + IEC 标签（B / KiB / MiB / GiB），与后端 MiB 口径一致；
 *  B/KB/MB 旧标签已退役（e2e 与单测钉住 `'超过单张上限 20 MiB'`）。
 *  舍入：KiB 取整；MiB ≥10 取整、<10 一位小数；GiB 一位小数。
 *  非有限 / 负值返回 '—'——宁可显"不可得"，也不编一个像样的数字。 */
export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} B`;
  const kib = bytes / 1024;
  if (kib < 1024) return `${Math.round(kib)} KiB`;
  const mib = kib / 1024;
  if (mib < 1024) return `${mib >= 10 ? Math.round(mib) : Math.round(mib * 10) / 10} MiB`;
  return `${Math.round((mib / 1024) * 10) / 10} GiB`;
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

/** 展示层统一 stringify：字符串原样，其余 JSON 两空格缩进。
 *  截断由 truncateForDisplay 负责，二者配套使用。 */
export function stringifyForDisplay(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}
