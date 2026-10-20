/** Timeline 甘特卡片流的**派生规则**（#1419）——纯函数，组件只负责渲染。
 *
 *  真相仍是 `conversation.events`（不变量 #22）：这里不新增数据源，只把事件里
 *  已有的时间/状态折叠成卡片需要的形状：
 *    - `start`：事件自己的 `time`（历史事件）或工具调用的 `started_at`（SSE 回退）；
 *    - `durationMs`：只取**可配对**的真实跨度（tool/call↔tool/result、
 *      model/request 的 `duration_ms`、delegation-started↔finished、run 起止）；
 *      不可得 = null，渲染层显示「—」，不编 0、不拿相邻事件间隔冒充耗时；
 *    - `status`：ok / fail / run / idle，失败筛选与状态色共用同一判据。
 *
 *  展开键值不在这里生成：统一复用 `eventKey()`（`lib/inspectorPanel.ts`）。
 */
import { EventType } from '../types';
import type { AgentEvent, ToolCall } from '../types';
import { eventKey } from './inspectorPanel';

export type TimelineStatus = 'ok' | 'fail' | 'run' | 'idle';
export type TimelineFilter = 'all' | 'tool' | 'model' | 'fail';
export type EventCategory = 'tool' | 'model' | 'agent' | 'run' | 'user' | 'other';

export interface EventTiming {
  /** 绝对 epoch ms；不可得 = null（卡片仍渲染，只是没有甘特位置）。 */
  start: number | null;
  /** 真实跨度；不可得 = null（右侧显示「—」）。 */
  durationMs: number | null;
  status: TimelineStatus;
}

export interface TimelineAxis {
  start: number;
  end: number;
  maxMs: number;
}

export interface SeqGap {
  /** 缺口**前一条**事件在 events 数组里的下标（渲染层在它后面插入横幅）。 */
  afterIndex: number;
  from: number;
  to: number;
  missing: number;
}

const FAIL_TYPES = new Set<string>([
  EventType.MODEL_FAILED,
  EventType.RUN_FAILED,
  EventType.ERROR,
  EventType.CONTEXT_COMPACTION_FAILED,
  EventType.COMPLETION_EVIDENCE_BLOCKED,
  EventType.TOOL_FAILURE_GUARD,
  EventType.GUARD_STUCK,
]);

const IDLE_TYPES = new Set<string>([
  EventType.RUN_INTERRUPTED,
  EventType.RUN_PAUSED,
  EventType.RUN_RESUMED,
  EventType.TOOL_APPROVAL_REQUESTED,
  EventType.OPERATION_RECONCILE_REQUIRED,
]);

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : null;
}

function timeMs(value: unknown): number | null {
  if (typeof value !== 'string' || !value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

function finiteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

/** `tool/result.data.content` 是后端 `ToolResult.model_dump_json()` 的字符串；
 *  兼容已解析对象（测试/未来契约）。`cancelled` 是中断 ≠ 失败（同投影口径）。 */
function toolResultOutcome(data: Record<string, unknown>): { ok: boolean | null; cancelled: boolean } {
  let parsed: unknown = data.content;
  if (typeof parsed === 'string') {
    try {
      parsed = JSON.parse(parsed);
    } catch {
      return { ok: null, cancelled: false };
    }
  }
  const record = asRecord(parsed);
  if (!record) return { ok: null, cancelled: false };
  const inner = asRecord(record.data);
  const cancelled = inner?.cancelled === true;
  return { ok: typeof record.ok === 'boolean' ? record.ok : null, cancelled };
}

function toolStatus(status: ToolCall['status'] | undefined): TimelineStatus {
  if (status === 'success') return 'ok';
  if (status === 'failed') return 'fail';
  if (status === 'stopped') return 'idle';
  if (status === 'running') return 'run';
  return 'idle';
}

function categoryOf(type: string): EventCategory {
  if (type.startsWith('tool/')) return 'tool';
  if (type.startsWith('model/') || type.startsWith('reasoning/') || type === EventType.TEXT_DELTA) {
    return 'model';
  }
  if (type.startsWith('agent/')) return 'agent';
  if (type.startsWith('run/') || type.startsWith('session/')) return 'run';
  if (type.startsWith('user/') || type.startsWith('steer/') || type.startsWith('message/')) return 'user';
  return 'other';
}

/** 事件分类（筛选 chips / 卡片图标共用）。 */
export function eventCategory(e: AgentEvent): EventCategory {
  return categoryOf(e.type);
}

function baseStatus(e: AgentEvent): TimelineStatus {
  if (FAIL_TYPES.has(e.type)) return 'fail';
  if (IDLE_TYPES.has(e.type)) return 'idle';
  if (e.type === EventType.RUN_COMPLETED) return 'ok';
  if (e.type === EventType.MODEL_COMPLETED) return 'ok';
  if (e.type === EventType.MODEL_REQUEST) {
    const outcome = e.data.outcome;
    if (outcome === 'failed' || outcome === 'error') return 'fail';
    if (outcome === 'completed' || outcome === 'success') return 'ok';
    return 'idle';
  }
  if (e.type === EventType.AGENT_DELEGATION_FINISHED) {
    const status = e.data.status;
    if (status === 'failed' || e.data.error != null) return 'fail';
    return 'ok';
  }
  if (e.type === EventType.TOOL_RESULT) {
    const outcome = toolResultOutcome(e.data);
    if (outcome.cancelled) return 'idle';
    if (outcome.ok === true) return 'ok';
    if (outcome.ok === false) return 'fail';
    return 'idle';
  }
  return 'idle';
}

/** 所有事件的 timing 表（键 = `eventKey`）。O(N)，调用方用 `useMemo` 收敛。 */
export function deriveEventTimings(
  events: readonly AgentEvent[],
  tools: readonly ToolCall[] = [],
): Map<string, EventTiming> {
  const toolById = new Map<string, ToolCall>();
  for (const tool of tools) toolById.set(tool.tool_call_id, tool);

  const resultByCall = new Map<string, AgentEvent>();
  const runEnd = new Map<string, AgentEvent>();
  const delegationEnd = new Map<string, AgentEvent>();
  const requestStarted = new Map<string, AgentEvent>();

  for (const e of events) {
    const id = typeof e.data.tool_call_id === 'string' ? e.data.tool_call_id : null;
    if (e.type === EventType.TOOL_RESULT && id) resultByCall.set(id, e);
    if (e.type === EventType.RUN_COMPLETED || e.type === EventType.RUN_FAILED || e.type === EventType.RUN_INTERRUPTED) {
      if (e.run_id) runEnd.set(e.run_id, e);
    }
    if (e.type === EventType.AGENT_DELEGATION_FINISHED) {
      const child = typeof e.data.child_session_id === 'string' ? e.data.child_session_id : null;
      if (child) delegationEnd.set(child, e);
    }
    if (e.type === EventType.MODEL_REQUEST_STARTED) {
      const requestId = typeof e.data.request_id === 'string' ? e.data.request_id : null;
      if (requestId) requestStarted.set(requestId, e);
    }
  }

  const timings = new Map<string, EventTiming>();
  for (const e of events) {
    const key = eventKey(e);
    const data = e.data;
    const callId = typeof data.tool_call_id === 'string' ? data.tool_call_id : null;
    const tool = callId ? toolById.get(callId) : undefined;

    let start = timeMs(e.time);
    if (start === null && tool) {
      start = timeMs(tool.started_at) ?? timeMs(tool.completed_at);
    }

    let durationMs: number | null = null;
    let status = baseStatus(e);

    if (e.type === EventType.TOOL_CALL && callId) {
      const result = resultByCall.get(callId);
      const resultAt = timeMs(result?.time) ?? timeMs(tool?.completed_at);
      if (start !== null && resultAt !== null && resultAt >= start) durationMs = resultAt - start;
      if (result) {
        status = baseStatus(result);
      } else if (tool) {
        status = toolStatus(tool.status);
      } else {
        status = 'run';
      }
    } else if (e.type === EventType.MODEL_REQUEST) {
      durationMs = finiteNumber(data.duration_ms);
      if (durationMs === null) {
        const requestId = typeof data.request_id === 'string' ? data.request_id : null;
        const started = requestId ? requestStarted.get(requestId) : undefined;
        const startedAt = started ? timeMs(started.time) : null;
        if (startedAt !== null && start !== null && start >= startedAt) durationMs = start - startedAt;
      }
    } else if (e.type === EventType.AGENT_DELEGATION_STARTED) {
      const child = typeof data.child_session_id === 'string' ? data.child_session_id : null;
      const end = child ? delegationEnd.get(child) : undefined;
      const endAt = end ? timeMs(end.time) : null;
      if (start !== null && endAt !== null && endAt >= start) durationMs = endAt - start;
      else status = 'run';
    } else if (e.type === EventType.RUN_STARTED && e.run_id) {
      const end = runEnd.get(e.run_id);
      const endAt = end ? timeMs(end.time) : null;
      if (start !== null && endAt !== null && endAt >= start) durationMs = endAt - start;
    }

    timings.set(key, { start, durationMs, status });
  }
  return timings;
}

export function eventMatchesFilter(
  e: AgentEvent,
  filter: TimelineFilter,
  timing: EventTiming | undefined,
): boolean {
  if (filter === 'all') return true;
  if (filter === 'fail') return timing?.status === 'fail';
  if (filter === 'tool') return eventCategory(e) === 'tool';
  return eventCategory(e) === 'model';
}

export interface FilterCounts {
  all: number;
  tool: number;
  model: number;
  fail: number;
}

/** 计数口径 = **当前时间线**（主会话）的全部事件；子会话在独立视图里，不并进主计数
 *  （定稿 notes §6 的「当前泳道」）。计数不受当前筛选影响——它回答「筛到会剩几条」。 */
export function filterCounts(
  events: readonly AgentEvent[],
  timings: ReadonlyMap<string, EventTiming>,
): FilterCounts {
  const counts: FilterCounts = { all: events.length, tool: 0, model: 0, fail: 0 };
  for (const e of events) {
    const category = eventCategory(e);
    if (category === 'tool') counts.tool += 1;
    else if (category === 'model') counts.model += 1;
    if (timings.get(eventKey(e))?.status === 'fail') counts.fail += 1;
  }
  return counts;
}

/** 会话时间轴边界：所有**可定位**事件的最早开始 → 最晚结束。无有效时间 = null
 *  （渲染层隐藏刻度与甘特条，而不是把序号当时间）。 */
export function timelineAxis(timings: ReadonlyMap<string, EventTiming>): TimelineAxis | null {
  let start = Number.POSITIVE_INFINITY;
  let end = Number.NEGATIVE_INFINITY;
  for (const timing of timings.values()) {
    if (timing.start === null) continue;
    start = Math.min(start, timing.start);
    const finish = timing.start + (timing.durationMs ?? 0);
    end = Math.max(end, finish);
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return { start, end, maxMs: end - start };
}

/** 条的位置/长度（百分比）：短事件至少 0.35% + CSS `min-width:8px`，保证可点可见
 *  （定稿 notes §6）。 */
export function barGeometry(
  timing: EventTiming,
  axis: TimelineAxis,
): { left: number; width: number; point: boolean } | null {
  if (timing.start === null) return null;
  const left = ((timing.start - axis.start) / axis.maxMs) * 100;
  const width = timing.durationMs === null ? 0 : (timing.durationMs / axis.maxMs) * 100;
  return {
    left: Math.max(0, Math.min(100, left)),
    width: Math.max(width, 0.35),
    point: timing.durationMs === null,
  };
}

/** seq 缺口（与 `StepDetail.seqGaps` 同判据，但保留插入位置与缺失条数）。 */
export function seqGapMarkers(events: readonly AgentEvent[]): SeqGap[] {
  const gaps: SeqGap[] = [];
  let prev: number | null = null;
  let prevIndex = -1;
  for (let i = 0; i < events.length; i++) {
    const seq = events[i].seq;
    if (seq === null || seq === undefined) continue;
    if (prev !== null && seq > prev + 1) {
      gaps.push({ afterIndex: prevIndex, from: prev, to: seq, missing: seq - prev - 1 });
    }
    prev = Math.max(prev ?? seq, seq);
    prevIndex = i;
  }
  return gaps;
}

/** 本地时钟 HH:MM:SS（卡片元信息；完整时间戳仍由 hover 浮层提供）。 */
export function formatClock(iso: string | null | undefined): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  const p = (n: number) => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** 轴刻度标签：0s / 15s / 1m / 1m30s——固定单位，不随 locale 漂移。 */
export function formatAxisLabel(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) {
    const seconds = ms / 1000;
    return seconds < 10 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds)}s`;
  }
  const total = Math.floor(ms / 1000);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return seconds === 0 ? `${minutes}m` : `${minutes}m${seconds}s`;
}
