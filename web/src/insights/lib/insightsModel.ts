/** 上下文洞察模块的纯函数层（#1420）：过滤 / 排序 / 分组 / 热力图网格 / 构成段。
 *
 *  无 DOM、无 React、无请求——组件只负责渲染，决策全部在这里可测。所有数字来自
 *  后端三组只读端点（`lib/api.ts`），本文件不估算、不补数（不变量 #22）。
 *
 *  **只 token 口径**：本模块没有任何成本 / 美元字段（票面裁决）。
 */

import { formatCoarseDuration } from '../../lib/format';
import type {
  ContextEventPoint,
  ContextRequestPoint,
  InsightsComposition,
  InsightsSessionRow,
} from '../../lib/api';

/** 六桶的展示元数据。顺序与既有 `ContextUsagePanel` 一致（用户已熟悉这套图例），
 *  颜色只引用 token——模块 CSS 不出现颜色字面量（AGENTS.md §15 / #1364）。 */
export const COMPOSITION_PARTS = [
  { key: 'messages', label: '消息', color: 'var(--accent)' },
  { key: 'system_prompt', label: '系统提示词', color: 'var(--info)' },
  { key: 'skills', label: '技能', color: 'var(--success)' },
  { key: 'tools_system', label: '系统工具', color: 'var(--warning)' },
  { key: 'tools_mcp', label: 'MCP 工具', color: 'var(--danger)' },
  { key: 'other', label: '其他', color: 'var(--text-tertiary)' },
] as const;

export type CompositionKey = (typeof COMPOSITION_PARTS)[number]['key'];

export interface CompositionSegment {
  key: CompositionKey;
  label: string;
  color: string;
  value: number;
  pct: number;
}

export function compositionTotal(composition: InsightsComposition): number {
  let total = 0;
  for (const part of COMPOSITION_PARTS) total += composition[part.key] || 0;
  return total;
}

/** 固定六桶 → 段（零值也保留在返回里，图例需要如实列出"这个桶是 0"）。
 *  `pct` 的分母是**构成合计**（Σ 六桶），与后端「分类求和 = 快照总量」同口径。 */
export function compositionSegments(composition: InsightsComposition): CompositionSegment[] {
  const total = compositionTotal(composition);
  return COMPOSITION_PARTS.map((part) => {
    const value = composition[part.key] || 0;
    return {
      key: part.key,
      label: part.label,
      color: part.color,
      value,
      pct: total > 0 ? (value / total) * 100 : 0,
    };
  });
}

// ── 数字 / 时间格式（展示层唯一实现）────────────────────────────────

export function formatCount(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  return value.toLocaleString();
}

/** 大数字的紧凑写法（环形图中心 / 图表轴）：≥1 万用「万」，否则千分位。 */
export function formatCompact(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '—';
  if (Math.abs(value) >= 100_000_000) return `${(value / 100_000_000).toFixed(1)}亿`;
  if (Math.abs(value) >= 10_000) return `${(value / 10_000).toFixed(1)}万`;
  return value.toLocaleString();
}

/** 缓存命中率：后端给的是 0..1 比例；null = 一次缓存明细都没上报（显示 —，不造 0%）。 */
export function formatRate(rate: number | null | undefined): string {
  if (typeof rate !== 'number' || !Number.isFinite(rate)) return '—';
  return `${(rate * 100).toFixed(1)}%`;
}

export function formatActiveMs(ms: number | null | undefined): string {
  if (typeof ms !== 'number' || !Number.isFinite(ms) || ms <= 0) return '—';
  return formatCoarseDuration(ms) ?? '—';
}

// ── 会话卡：过滤 / 排序 / 分组 ─────────────────────────────────────

export type SessionSort = 'recent' | 'tokens' | 'context';

export const SESSION_SORTS: ReadonlyArray<{ key: SessionSort; label: string }> = [
  { key: 'recent', label: '最近' },
  { key: 'tokens', label: '用量' },
  { key: 'context', label: '上下文大小' },
];

export interface SessionFilter {
  /** 客户端搜索：session_id / 首条用户消息（title）/ workspace。 */
  text: string;
  /** 热力图钉住的本地日期（YYYY-MM-DD）；null = 不按日过滤。 */
  day: string | null;
}

/** ISO 时间 → 浏览器本地日期键（YYYY-MM-DD）。无效 / 缺失 → null（该行不参与日过滤）。 */
export function localDayKey(iso: string | null): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

export function filterSessions(
  rows: readonly InsightsSessionRow[],
  filter: SessionFilter,
): InsightsSessionRow[] {
  const needle = filter.text.trim().toLowerCase();
  return rows.filter((row) => {
    if (filter.day !== null && localDayKey(row.last_active_at) !== filter.day) return false;
    if (needle === '') return true;
    return (
      row.session_id.toLowerCase().includes(needle)
      || row.title.toLowerCase().includes(needle)
      || (row.workspace ?? '').toLowerCase().includes(needle)
    );
  });
}

/** 排序。`context` 需要外部提供的上下文大小（来自既有 `/context-usage` 的懒加载）；
 *  还没取到（undefined）的会话排在后面——不拿别的字段冒充上下文大小。 */
export function sortSessions(
  rows: readonly InsightsSessionRow[],
  sort: SessionSort,
  contextSizes?: Readonly<Record<string, number | null | undefined>>,
): InsightsSessionRow[] {
  const byRecent = (a: InsightsSessionRow, b: InsightsSessionRow) =>
    (b.last_active_at ?? '').localeCompare(a.last_active_at ?? '');
  const out = [...rows];
  out.sort((a, b) => {
    if (sort === 'tokens') {
      const diff = b.billed_tokens - a.billed_tokens;
      return diff !== 0 ? diff : byRecent(a, b);
    }
    if (sort === 'context') {
      const av = contextSizes?.[a.session_id];
      const bv = contextSizes?.[b.session_id];
      const aKnown = typeof av === 'number' && Number.isFinite(av);
      const bKnown = typeof bv === 'number' && Number.isFinite(bv);
      if (aKnown && bKnown && av !== bv) return (bv as number) - (av as number);
      if (aKnown !== bKnown) return aKnown ? -1 : 1;
      return byRecent(a, b);
    }
    return byRecent(a, b);
  });
  return out;
}

export interface SessionGroup {
  /** null = 未分组（后端 `workspace` 为空）。 */
  workspace: string | null;
  rows: InsightsSessionRow[];
}

/** 按工作区分组；组序 = 该组第一条会话在已排序列表里的位置（未分组永远最后）。
 *
 *  为什么不按工作区名字母序：排序是「最近 / 用量 / 上下文大小」，组序若另按字母排，
 *  用户切到「用量」后第一组可能不是用量最高的那组，排序看起来像没生效。按首次出现
 *  排序让组序跟随排序键；组内顺序也已由 `sortSessions` 定好。 */
export function groupSessions(rows: readonly InsightsSessionRow[]): SessionGroup[] {
  const groups = new Map<string | null, InsightsSessionRow[]>();
  for (const row of rows) {
    const key = row.workspace?.trim() || null;
    const bucket = groups.get(key);
    if (bucket) bucket.push(row);
    else groups.set(key, [row]);
  }
  const ungrouped = groups.get(null);
  const entries = [...groups.entries()].filter(([workspace]) => workspace !== null);
  if (ungrouped) entries.push([null, ungrouped]);
  return entries.map(([workspace, groupRows]) => ({ workspace, rows: groupRows }));
}

// ── 8 周热力图 ──────────────────────────────────────────────────────

export interface HeatCell {
  /** YYYY-MM-DD */
  key: string;
  tokens: number;
  future: boolean;
}

export interface HeatColumn {
  cells: HeatCell[];
  /** 该列要显示的月份标签（1-12）；null = 不显示。 */
  month: number | null;
}

function parseDayKey(key: string): Date | null {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(key);
  if (!match) return null;
  const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12);
  return Number.isNaN(date.getTime()) ? null : date;
}

function dayKeyOf(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** 以 `today` 所在周（周日为首）为最后一列，向前铺 `weeks` 列、每列 7 天。
 *  `today` 无效 / 窗口算不出来 → null（调用方降级为空态，不画一张假网格）。 */
export function heatmapColumns(
  points: readonly { date: string; tokens: number }[],
  today: string,
  weeks = 8,
): HeatColumn[] | null {
  const todayDate = parseDayKey(today);
  if (todayDate === null || weeks < 1) return null;
  const byDay = new Map(points.map((point) => [point.date, point.tokens]));
  const lastSunday = new Date(todayDate);
  lastSunday.setDate(lastSunday.getDate() - lastSunday.getDay());
  const firstSunday = new Date(lastSunday);
  firstSunday.setDate(firstSunday.getDate() - 7 * (weeks - 1));
  const columns: HeatColumn[] = [];
  let previousMonth: string | null = null;
  for (let w = 0; w < weeks; w += 1) {
    const sunday = new Date(firstSunday);
    sunday.setDate(sunday.getDate() + 7 * w);
    const cells: HeatCell[] = [];
    for (let d = 0; d < 7; d += 1) {
      const date = new Date(sunday);
      date.setDate(date.getDate() + d);
      const key = dayKeyOf(date);
      cells.push({
        key,
        tokens: byDay.get(key) ?? 0,
        future: key > today,
      });
    }
    // 月标签：这一列的周日所属月份与上一列不同 → 标出新月份。
    // 跨月的周（如 8/30–9/5）标 8 月；下一列 9/6 再标 9 月，月份不会丢。
    const monthKey = cells[0].key.slice(0, 7);
    const month = monthKey !== previousMonth ? Number(monthKey.slice(5, 7)) : null;
    previousMonth = monthKey;
    columns.push({ cells, month });
  }
  return columns;
}

/** 热力深度：0（无数据）→ 4（窗口内最大值）。与 dsh-context 的四档同构。 */
export function heatLevel(tokens: number, max: number): 0 | 1 | 2 | 3 | 4 {
  if (tokens <= 0 || max <= 0) return 0;
  const ratio = tokens / max;
  if (ratio <= 0.25) return 1;
  if (ratio <= 0.5) return 2;
  if (ratio <= 0.75) return 3;
  return 4;
}

export function heatmapMax(points: readonly { tokens: number }[]): number {
  let max = 0;
  for (const point of points) if (point.tokens > max) max = point.tokens;
  return max;
}

/** 热力图窗口的「今天」：后端返回的最后一天（恒为本地今天）；没有数据时用浏览器本地日期。 */
export function heatmapToday(points: readonly { date: string }[]): string {
  const last = points[points.length - 1]?.date;
  return last ?? dayKeyOf(new Date());
}

// ── 逐请求趋势 ─────────────────────────────────────────────────────

/** 上下文事件挂到「它之后的第一条请求」上（dsh 的 attachMarkers 同语义）。 */
export function attachEventMarkers(
  requests: readonly ContextRequestPoint[],
  events: readonly ContextEventPoint[],
): Array<ContextEventPoint | null> {
  const markers: Array<ContextEventPoint | null> = requests.map(() => null);
  for (const event of events) {
    const index = requests.findIndex((request) => request.seq >= event.seq);
    if (index >= 0 && markers[index] === null) markers[index] = event;
  }
  return markers;
}

export interface TrendScale {
  maxUp: number;
  maxDown: number;
}

/** 趋势图纵向尺度：total 模式只看 total；delta 模式分开正负（负值不能画到零线上方）。 */
export function trendScale(
  requests: readonly ContextRequestPoint[],
  mode: 'total' | 'delta',
): TrendScale {
  let maxUp = 0;
  let maxDown = 0;
  for (const request of requests) {
    const value = request.total_tokens;
    if (mode === 'delta') {
      if (value > maxUp) maxUp = value;
      if (-value > maxDown) maxDown = -value;
    } else if (value > maxUp) {
      maxUp = value;
    }
  }
  return { maxUp, maxDown };
}

/** 请求明细里的一行：`key: value` 对（组件只排版，不再做口径判断）。 */
export function requestFacts(request: ContextRequestPoint): Array<{ label: string; value: string }> {
  return [
    { label: 'prompt', value: formatCount(request.prompt_tokens) },
    { label: 'completion', value: formatCount(request.completion_tokens) },
    { label: '缓存命中', value: formatCount(request.cached_tokens) },
    { label: 'total', value: formatCount(request.total_tokens) },
    { label: '耗时', value: formatCoarseDuration(request.duration_ms) ?? '—' },
  ];
}

// ── 上下文事件 / Fleet 文案 ────────────────────────────────────────

export const CONTEXT_EVENT_LABELS: Readonly<Record<string, string>> = {
  'context/compacted': '上下文已压缩',
  'compaction/start': '压缩开始',
  'compaction/end': '压缩结束',
  'context/window-changed': '上下文窗口变更',
};

export const FLEET_STATUS_LABELS: Readonly<Record<string, string>> = {
  running: '运行中',
  idle: '空闲',
  completed: '已完成',
  failed: '失败',
  stopped: '已停止',
};

/** 上下文窗口阈值线（后端 `thresholds` 是 0..1 比例）。标签写死「比例阈值」，
 *  因为 Owlet 没有 dsh 的 headroom / reserved output，不能画成 token 预留。 */
export function thresholdMarks(thresholds: { auto_compact: number; hard_guard: number }): Array<{
  ratio: number;
  label: string;
}> {
  return [
    { ratio: thresholds.auto_compact, label: '自动压缩 70%' },
    { ratio: thresholds.hard_guard, label: '硬上限 85%' },
  ];
}
