/** StepDetail — right rail Run Inspector（PRD §8，ADR-0014 D3 重排）.
 *
 * Timeline 升为 Inspector 常驻主体（PRD §8.3 "Timeline 是 Inspector 的核心"）：
 *   Timeline  → 每条事件 verbatim（seq · type · summary），默认 tab；点击行钻取
 *               事件详情，并反向定位中间主区（PRD §9.2 联动）
 *   Overview  → Run 级摘要（RUN / TOOLS / CONTEXT / MODEL / TRACE——原 Chat tab）
 *   Changes   → 文件 diff 聚合（保留，D11 不删）
 *   Terminal  → bash 调用聚合（保留）
 *   Artifacts → artifact ref 聚合（保留）
 *
 * Event-level inspector（PRD §8.4 四段）：Overview / Input / Output / Raw，
 * 顶部返回按钮回 Timeline（无弹窗——"上下文 Inspector"）。
 */

import { memo, useEffect, useRef, useState } from 'react';
import type { MouseEvent as ReactMouseEvent } from 'react';
import {
  AlertTriangle, ArrowLeft, ChevronRight, Clock, Database, FileCheck2, FileDiff,
  Hash, Layers, ListTree, Package, TerminalSquare,
} from 'lucide-react';
import type { AgentEvent, ConversationState, ToolCall } from '../types';
import { EventType } from '../types';
import { formatDuration, formatTimestamp, stringifyForDisplay, truncateForDisplay } from '../lib/format';
import { summarizeEvent } from '../lib/projection';
import { deriveRunPulse } from '../lib/runState';
import { CopyButton } from './CopyButton';
import { JsonTree } from './JsonTree';

/** Inspector focus: Run-level overview or a drilled-in event. */
export type InspectorFocus =
  | { kind: 'run' }
  | { kind: 'tool'; tool: ToolCall }
  | { kind: 'event'; event: AgentEvent };

type Tab = 'timeline' | 'chat' | 'changes' | 'terminal' | 'artifacts';

const TABS: readonly { id: Tab; label: string }[] = [
  { id: 'timeline', label: 'Timeline' },
  { id: 'chat', label: 'Overview' },
  { id: 'changes', label: 'Changes' },
  { id: 'terminal', label: 'Terminal' },
  { id: 'artifacts', label: 'Artifacts' },
];

// C4 Timeline hover 浮层布局常量（与 .tl-tooltip CSS 对齐——行高 16、内距 12、
// 与行边的 4px 间隙、上方放置需要的 16px 顶部裕量）。改一个地方即可。
const TIP_LINE_H = 16;
const TIP_PADDING = 12;
const TIP_GAP = 4;
const TIP_TOP_MARGIN = 16;

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  focus: InspectorFocus;
  onFocusRun: () => void;
  onFocusTool: (tool: ToolCall) => void;
  onFocusEvent: (event: AgentEvent) => void;
  /** PRD §9.2 反向联动：点 Timeline 行 → 中间主区滚动定位对应事件。 */
  onJumpToStream?: (event: AgentEvent) => void;
}

export function StepDetail({ conversation, streaming, focus, onFocusRun, onFocusTool, onFocusEvent, onJumpToStream }: Props) {
  const [tab, setTab] = useState<Tab>('timeline');

  if (!conversation) {
    return (
      <aside className="step-detail">
        <DetailEmpty />
      </aside>
    );
  }

  if (focus.kind !== 'run') {
    return (
      <aside className="step-detail">
        <div className="detail-header">
          <button className="detail-back-btn" onClick={onFocusRun}>
            <ArrowLeft size={12} /> 返回 Timeline
          </button>
          <span className="detail-focus-type">
            {focus.kind === 'tool' ? focus.tool.name : focus.event.type}
          </span>
        </div>
        <EventInspector focus={focus} />
      </aside>
    );
  }

  const tools = conversation.turns.flatMap((t) => t.tools);
  const pulse = deriveRunPulse(conversation, streaming);

  return (
    <aside className="step-detail">
      <div className="detail-header">
        <span className="panel-label">Run Inspector</span>
        <span className={`run-badge run-badge-${pulse.state}`}>{pulse.label}</span>
        {/* PRD §8.2：Run ID 常驻头部（短码，完整 ID 在 Overview） */}
        <span className="detail-run-id mono num" title={`session ${conversation.session_id}`}>
          {conversation.session_id.slice(0, 8)}
        </span>
      </div>

      <div className="detail-tabs" role="tablist" aria-label="Inspector 视图">
        {TABS.map((t) => (
          <button
            key={t.id}
            role="tab"
            aria-selected={tab === t.id}
            className={`detail-tab ${tab === t.id ? 'sel' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'chat' && <ChatTab conversation={conversation} tools={tools} onFocusTool={onFocusTool} />}
      {tab === 'timeline' && (
        <TimelineTab
          key={conversation.session_id}
          conversation={conversation}
          onFocusEvent={onFocusEvent}
          onJumpToStream={onJumpToStream}
        />
      )}
      {tab === 'changes' && <ChangesTab tools={tools} />}
      {tab === 'terminal' && <TerminalTab tools={tools} onFocusTool={onFocusTool} />}
      {tab === 'artifacts' && <ArtifactsTab tools={tools} />}
    </aside>
  );
}

// ── Chat tab：Run 级摘要（真数据区块 + 空槽标注） ──

function ChatTab({
  conversation, tools, onFocusTool,
}: {
  conversation: ConversationState;
  tools: ToolCall[];
  onFocusTool: (tool: ToolCall) => void;
}) {
  const runStart = conversation.events.find((e) => e.type === EventType.RUN_STARTED)?.time;
  const runEnd = conversation.events.find((e) => e.type === EventType.RUN_COMPLETED || e.type === EventType.RUN_FAILED)?.time;
  const runDuration = formatDuration(runStart, runEnd);

  return (
    <>
      <div className="detail-section">
        <div className="detail-section-title">
          <Hash size={12} /> RUN
        </div>
        <div className="detail-row">
          <span className="detail-key">id</span>
          <code className="detail-val detail-val-mono">{conversation.session_id.slice(0, 16)}</code>
        </div>
        <div className="detail-row">
          <span className="detail-key">轮次</span>
          <span className="detail-val">{conversation.turns.length}</span>
        </div>
        {runStart && (
          <div className="detail-row">
            <span className="detail-key">开始</span>
            <span className="detail-val">{new Date(runStart).toLocaleTimeString()}</span>
          </div>
        )}
        <div className="detail-row">
          <span className="detail-key">耗时</span>
          <span className="detail-val">{runDuration ?? '—'}</span>
        </div>
        {/* 后端 Gap 2：trace_id 恒 null（Langfuse Phase 15 接入）→「未追踪」灰字，
            属预期降级而非故障；跳转链接待 Phase 15 一并加（Scope Lock：不预做）。 */}
        <div className="detail-row">
          <span className="detail-key">Trace</span>
          {conversation.trace_id ? (
            <code className="detail-val detail-val-mono">{conversation.trace_id}</code>
          ) : (
            <span className="detail-val detail-val-muted">未追踪</span>
          )}
        </div>
      </div>

      <div className="detail-section">
        <div className="detail-section-title">
          <Package size={12} /> TOOLS
        </div>
        <div className="detail-row">
          <span className="detail-key">计数</span>
          <span className="detail-val">{tools.length}</span>
        </div>
        <div className="detail-row">
          <span className="detail-key">活跃</span>
          <span className="detail-val">{tools.filter((t) => t.status === 'running').length}</span>
        </div>
        <div className="detail-row">
          <span className="detail-key">失败</span>
          <span className="detail-val">{tools.filter((t) => t.status === 'failed').length}</span>
        </div>
        {tools.map((t) => (
          <button key={t.tool_call_id} className="detail-tool-row" onClick={() => onFocusTool(t)}>
            <ChevronRight size={12} />
            <span className={`tool-status-dot tool-status-dot-${t.status}`} />
            <span className="detail-tool-name">{t.name}</span>
          </button>
        ))}
      </div>

      {conversation.compactions.length > 0 && (
        <div className="detail-section">
          <div className="detail-section-title">
            <Layers size={12} /> CONTEXT
          </div>
          {conversation.compactions.map((c, i) => (
            <div key={i} className="detail-compaction-row">
              <div className="detail-row">
                <span className="detail-key">压缩轮次</span>
                <span className="detail-val">{c.compacted_turn_count}</span>
              </div>
              <div className="detail-row">
                <span className="detail-key">Token 估算</span>
                <span className="detail-val">{c.token_estimate.toLocaleString()}</span>
              </div>
              {c.fallback_used && (
                <div className="detail-row detail-row-warn">
                  <AlertTriangle size={11} /> <span>兜底降级（非模型摘要）</span>
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {conversation.reconcile_queue.length > 0 && (
        <div className="detail-section">
          <div className="detail-section-title">
            <AlertTriangle size={12} /> 需人工裁决
          </div>
          {conversation.reconcile_queue.map((r, i) => (
            <div key={i} className="detail-reconcile-row">
              <div className="detail-row">
                <span className="detail-key">工具</span>
                <span className="detail-val">{r.tool_name}</span>
              </div>
              <div className="detail-row">
                <span className="detail-key">参数身份</span>
                <code className="detail-val detail-val-mono">{r.args_identity}</code>
              </div>
              <div className="detail-row">
                <span className="detail-key">状态</span>
                <span className="detail-val detail-val-tag">{r.state}</span>
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="detail-section">
        <div className="detail-section-title">
          <ListTree size={12} /> TRACE
        </div>
        <div className="detail-row">
          <span className="detail-key">事件数</span>
          <span className="detail-val">{conversation.events.length}</span>
        </div>
        {conversation.unknown_events.length > 0 && (
          <div className="detail-row detail-row-warn">
            <span className="detail-key">未知事件</span>
            <span className="detail-val">{conversation.unknown_events.length}</span>
          </div>
        )}
        {seqGaps(conversation.events).map((gap, i) => (
          <div key={i} className="detail-row detail-row-warn">
            <span className="detail-key">seq 缺口</span>
            <span className="detail-val detail-val-mono">{gap}</span>
          </div>
        ))}
      </div>

      {/* MODEL：后端 Gap 1 已落地（model/completed + run/completed 观测字段）。
          全部缺失时保留空槽语义（提示而非留白），有任一真值则逐行渲染，
          缺失行显示「—」——绝不伪造 0（零伪造指标冻结决策）。 */}
      {conversation.model === null && conversation.usage_total === null && conversation.cost_usd === null ? (
        <div className="detail-section detail-reserved">
          <div className="detail-section-title">
            <Database size={12} /> MODEL
          </div>
          <div className="detail-empty-hint">暂无观测数据（等待 model/completed）</div>
        </div>
      ) : (
        <div className="detail-section">
          <div className="detail-section-title">
            <Database size={12} /> MODEL
          </div>
          <div className="detail-row">
            <span className="detail-key">模型</span>
            <span className="detail-val detail-val-mono">{conversation.model ?? '—'}</span>
          </div>
          <div className="detail-row">
            <span className="detail-key">用量</span>
            <span className="detail-val">
              {conversation.usage_total
                ? `${conversation.usage_total.total_tokens.toLocaleString()} tok（${conversation.usage_total.prompt_tokens.toLocaleString()} + ${conversation.usage_total.completion_tokens.toLocaleString()}）`
                : '—'}
            </span>
          </div>
          <div className="detail-row">
            <span className="detail-key">成本</span>
            <span className="detail-val">{conversation.cost_usd !== null ? `$${conversation.cost_usd}` : '—'}</span>
          </div>
        </div>
      )}
      <div className="detail-section detail-reserved">
        <div className="detail-section-title">
          <Database size={12} /> CHECKPOINT
        </div>
        <div className="detail-empty-hint">后端未暴露（无 API，集成阶段处理）</div>
      </div>
    </>
  );
}

// ── Timeline tab：事件真序日志（真相源 conversation.events，零过滤） ──

/** 尾窗默认大小 / 「加载更早」步长（P1-4）。200 行 ≈4ms 全量渲染（实测），
 * 40fps 合帧下余量充足；步长 500 一次多翻约 2.5 屏。 */
export const TIMELINE_WINDOW_DEFAULT = 200;
export const TIMELINE_WINDOW_STEP = 500;

/** seq 跳转检测（Inspector Scope "TRACE 事件计数 + seq 跳转"）：
 *  返回相邻可比较 seq 对之间的缺口描述（"12 → 15"），不可比较（null/乱序）则跳过。 */
export function seqGaps(events: AgentEvent[]): string[] {
  const gaps: string[] = [];
  let prev: number | null = null;
  for (const e of events) {
    if (e.seq === null) continue;
    if (prev !== null && e.seq > prev + 1) gaps.push(`${prev} → ${e.seq}`);
    prev = Math.max(prev ?? e.seq, e.seq);
  }
  return gaps;
}

/** 事件行的单行摘要——单一投影源（lib/projection.ts summarizeEvent）。 */
const eventSummary = summarizeEvent;

/** Timeline 行 hover 浮层内容（C4）：完整时间戳（含毫秒，本地时区）+ step。
 *  纯函数导出以便 SSR 测试锁定；行内已显示 seq/type，浮层只补看不到的。
 *  step_id 语义：null = 无归属（recover 合成事件等哨兵），不渲染行；
 *  数值（含 0，后端从 1 起但类型契约为 number）按合法 step 渲染。 */
export function formatEventTooltip(e: AgentEvent): string[] {
  const lines: string[] = [];
  const ts = formatTimestamp(e.time);
  if (ts) lines.push(ts);
  if (e.step_id !== null) lines.push(`step ${e.step_id}`);
  return lines;
}

interface TipState {
  x: number;
  y: number;
  lines: string[];
}

export function TimelineTab({ conversation, onFocusEvent, onJumpToStream }: { conversation: ConversationState; onFocusEvent: (e: AgentEvent) => void; onJumpToStream?: (e: AgentEvent) => void }) {
  // 尾窗裁剪（P1-4，DSH "cropped client views"）：真相全量留在 conversation.events
  // （不变量 #22 不动），视图只渲染最近窗口。实测依据：2k 全量渲染 40ms、20k 359ms
  // （流式合帧 40fps 下 Timeline tab 每秒烧 14s CPU）——200 行窗口 ≈4ms，流畅。
  const total = conversation.events.length;
  const [windowSize, setWindowSize] = useState(TIMELINE_WINDOW_DEFAULT);
  // C4：hover 时间戳浮层——单元素 fixed 浮层 + 容器事件委托（零每行 handler）。
  const [tip, setTip] = useState<TipState | null>(null);
  const visibleRef = useRef<AgentEvent[]>([]);
  const lastRowRef = useRef<HTMLElement | null>(null);

  // 滚动/缩放即隐藏（fixed 定位不随容器滚动，留着会错位）。
  useEffect(() => {
    const hide = () => {
      lastRowRef.current = null;
      setTip(null);
    };
    window.addEventListener('scroll', hide, true);
    window.addEventListener('resize', hide);
    return () => {
      window.removeEventListener('scroll', hide, true);
      window.removeEventListener('resize', hide);
    };
  }, []);

  if (total === 0) {
    return <TabEmpty hint="本会话尚无事件。" />;
  }
  const hidden = Math.max(0, total - windowSize);
  const visible = hidden > 0 ? conversation.events.slice(hidden) : conversation.events;
  visibleRef.current = visible;

  const handleOver = (e: ReactMouseEvent<HTMLDivElement>) => {
    const btn = (e.target as HTMLElement).closest?.('[data-tl-i]') as HTMLElement | null;
    if (btn === lastRowRef.current) return;
    lastRowRef.current = btn;
    if (!btn) {
      setTip(null);
      return;
    }
    const ev = visibleRef.current[Number(btn.dataset.tlI)];
    const lines = ev ? formatEventTooltip(ev) : [];
    if (lines.length === 0) {
      setTip(null);
      return;
    }
    const r = btn.getBoundingClientRect();
    const tipH = lines.length * TIP_LINE_H + TIP_PADDING;
    const above = r.top > tipH + TIP_TOP_MARGIN;
    setTip({ x: Math.max(8, r.left + 6), y: above ? r.top - tipH - TIP_GAP : r.bottom + TIP_GAP, lines });
  };
  const handleLeave = () => {
    lastRowRef.current = null;
    setTip(null);
  };

  return (
    <div className="detail-timeline" onMouseOver={handleOver} onMouseLeave={handleLeave}>
      {hidden > 0 && (
        <div className="timeline-window-bar">
          <button
            className="timeline-earlier"
            onClick={() => setWindowSize((w) => w + TIMELINE_WINDOW_STEP)}
          >
            加载更早 {Math.min(TIMELINE_WINDOW_STEP, hidden)} 条
          </button>
          <span className="timeline-window-hint">
            显示最近 {visible.length} / 共 {total} 条（前段已折叠，真相完整保留）
          </span>
        </div>
      )}
      {visible.map((e, i) => (
        /* key = 数组绝对下标：稳定性依赖 P0-1 的 append-only 事件契约
         * （events 只追加不重排/删除，见 HANDOFF_PERF_FRONTEND §9 P0-1）。 */
        <TimelineRow
          key={hidden + i}
          index={i}
          event={e}
          onFocusEvent={onFocusEvent}
          onJumpToStream={onJumpToStream}
        />
      ))}
      {tip && (
        <div className="tl-tooltip" role="tooltip" style={{ left: tip.x, top: tip.y }}>
          {tip.lines.map((l, i) => (
            <div key={i} className="tl-tooltip-line">
              {l}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// memo：投影层 events 数组为追加式（既有事件引用稳定），流式期间新 delta 到达时
// 旧行跳过 summarizeEvent 重算——只有新增行参与渲染。
const TimelineRow = memo(function TimelineRow({
  index,
  event,
  onFocusEvent,
  onJumpToStream,
}: {
  /** 可见窗口内下标（hover 浮层经 data-tl-i 反查事件）。 */
  index: number;
  event: AgentEvent;
  onFocusEvent: (e: AgentEvent) => void;
  onJumpToStream?: (e: AgentEvent) => void;
}) {
  return (
    <button
      className="timeline-row"
      data-tl-i={index}
      onClick={() => {
        onFocusEvent(event);
        // PRD §9.2 反向联动：选中行同时定位中间主区（App 层处理 pulse）。
        onJumpToStream?.(event);
      }}
    >
      <span className="tl-seq">{event.seq ?? '·'}</span>
      <span className="tl-type">{event.type}</span>
      <span className="tl-summary">{eventSummary(event)}</span>
    </button>
  );
});

// ── Changes tab：diff 双栏聚合（复用 ToolCard diff 形态的数据与 .diff-cols 形状） ──

function ChangesTab({ tools }: { tools: ToolCall[] }) {
  const diffs = tools.filter((t) => t.diff);
  if (diffs.length === 0) {
    return <TabEmpty hint="本次会话未产生文件变更。" />;
  }
  return (
    <>
      {diffs.map((t) => (
        <div key={t.tool_call_id} className="detail-section">
          <div className="detail-section-title">
            <FileDiff size={12} /> {t.name}: {String(t.args.path ?? '')}
          </div>
          <div className="diff-cols">
            <div className="diff-col diff-before">
              <div className="diff-col-label">变更前</div>
              <pre>{t.diff!.before || '（空）'}</pre>
            </div>
            <div className="diff-col diff-after">
              <div className="diff-col-label">变更后</div>
              <pre>{t.diff!.after || '（空）'}</pre>
            </div>
          </div>
          {t.diff!.truncated && <div className="detail-empty-hint">内容过长，已截断</div>}
        </div>
      ))}
    </>
  );
}

// ── Terminal tab：bash 调用聚合（命令执行面） ──

/** bash ToolResult 的后端形状（spec 04：exit_code + stdout）。形状不符返回 null。 */
function bashResult(tool: ToolCall): { exit_code?: number; stdout?: string } | null {
  if (typeof tool.result !== 'object' || tool.result === null) return null;
  const r = tool.result as Record<string, unknown>;
  return {
    exit_code: typeof r.exit_code === 'number' ? r.exit_code : undefined,
    stdout: typeof r.stdout === 'string' ? r.stdout : undefined,
  };
}

function TerminalTab({ tools, onFocusTool }: { tools: ToolCall[]; onFocusTool: (t: ToolCall) => void }) {
  const bashes = tools.filter((t) => t.name === 'bash');
  if (bashes.length === 0) {
    return <TabEmpty hint="本次会话未执行命令。" />;
  }
  return (
    <>
      {bashes.map((t) => {
        const result = bashResult(t);
        return (
          <button key={t.tool_call_id} className="detail-terminal-row" onClick={() => onFocusTool(t)}>
            <div className="detail-terminal-cmd">
              <span className="bash-prompt">$</span>
              <code>{String(t.args.command ?? '')}</code>
            </div>
            {result?.stdout !== undefined && <pre className="detail-terminal-out">{truncateForDisplay(result.stdout)}</pre>}
            {result?.exit_code !== undefined && (
              <span className={`exit-badge ${result.exit_code === 0 ? 'exit-ok' : 'exit-err'}`}>exit {result.exit_code}</span>
            )}
          </button>
        );
      })}
    </>
  );
}

// ── Artifacts tab：artifact 聚合页（工具挂载 ref，单一投影源——不变量 #22） ──

function ArtifactsTab({ tools }: { tools: ToolCall[] }) {
  // Artifacts only reach this tab through the projection attaching an ArtifactRef
  // to the producing ToolCall (lib/projection.ts ARTIFACT_CREATED case). If an
  // artifact/created event's tool isn't found by the projection, that's a
  // projection concern — not something to paper over here with a second truth
  // path (invariant #22).
  const artifacts = tools.filter((t) => t.artifact);
  if (artifacts.length === 0) {
    return <TabEmpty hint="本次会话未产生 Artifact。" />;
  }
  return (
    <>
      {artifacts.map((t) => (
        <div key={t.tool_call_id} className="detail-section">
          <div className="detail-section-title">
            <FileCheck2 size={12} /> {t.name}
          </div>
          <div className="detail-row">
            <span className="detail-key">ID</span>
            <code className="detail-val detail-val-mono">{t.artifact!.artifact_id.slice(0, 16)}</code>
          </div>
          <div className="detail-row">
            <span className="detail-key">大小</span>
            <span className="detail-val">{formatBytes(t.artifact!.size)}</span>
          </div>
          <div className="detail-row">
            <span className="detail-key">类型</span>
            <span className="detail-val detail-val-mono">{t.artifact!.mime_type}</span>
          </div>
        </div>
      ))}
    </>
  );
}

// ── 事件级 Inspector：Overview / Input / Output / Raw 四段（PRD §8.4） ──

/** 事件级 Inspector 主体——只接收已收窄的非 Run 焦点。 */
type EventFocus = Exclude<InspectorFocus, { kind: 'run' }>;

/** 事件级四段 tab 状态：Overview（元信息）/ Input（data）/ Output（同 data，语义入口）/
 *  Raw（完整事件）。Input 与 Output 对普通事件都是 event.data——Output 作为默认段
 *  保留"看结果优先"的旧习惯；Raw 是完整事件 JSON。 */
type EventIoTab = 'overview' | 'input' | 'output' | 'raw';

function EventInspector({ focus }: { focus: EventFocus }) {
  // hooks 规则：useState 必须在条件 return 之前（tool focus 分支也保持 hook 顺序稳定）
  const [tab, setTab] = useState<EventIoTab>('overview');
  if (focus.kind === 'tool') {
    return <ToolEventSections tool={focus.tool} />;
  }
  const event = focus.event;
  const dataJson = JSON.stringify(event.data, null, 2);
  const rawJson = JSON.stringify(event, null, 2);

  const tabs: readonly { id: EventIoTab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'input', label: 'Input' },
    { id: 'output', label: 'Output' },
    { id: 'raw', label: 'Raw' },
  ];

  return (
    <>
      <div className="io-tabs" role="tablist" aria-label="事件详情段">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`io-tab${tab === t.id ? ' sel' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <div className="detail-section">
          <div className="detail-section-title">
            <Clock size={12} /> {event.type}
          </div>
          {event.seq !== null && (
            <div className="detail-row">
              <span className="detail-key">seq</span>
              <span className="detail-val detail-val-mono num">{event.seq}</span>
            </div>
          )}
          {event.time && (
            <div className="detail-row">
              <span className="detail-key">time</span>
              <span className="detail-val detail-val-mono">{event.time}</span>
            </div>
          )}
          {event.step_id !== null && (
            <div className="detail-row">
              <span className="detail-key">step</span>
              <span className="detail-val num">{event.step_id}</span>
            </div>
          )}
          {event.run_id && (
            <div className="detail-row">
              <span className="detail-key">run</span>
              <span className="detail-val detail-val-mono">{event.run_id.slice(0, 16)}</span>
            </div>
          )}
          {event.event_id && (
            <div className="detail-row">
              <span className="detail-key">event_id</span>
              <span className="detail-val detail-val-mono">{event.event_id.slice(0, 16)}</span>
            </div>
          )}
        </div>
      )}
      {tab === 'input' && (
        <div className="detail-section">
          <div className="detail-section-title">Input (data)</div>
          <div className="detail-code-wrap">
            <CopyButton text={dataJson} label="复制 JSON" />
            <div className="detail-json">
              <JsonTree value={event.data} />
            </div>
          </div>
        </div>
      )}
      {tab === 'output' && (
        <div className="detail-section">
          <div className="detail-section-title">Output (data)</div>
          <div className="detail-code-wrap">
            <CopyButton text={dataJson} label="复制 JSON" />
            <div className="detail-json">
              <JsonTree value={event.data} />
            </div>
          </div>
        </div>
      )}
      {tab === 'raw' && (
        <div className="detail-section">
          <div className="detail-section-title">Raw</div>
          <div className="detail-code-wrap">
            <CopyButton text={rawJson} label="复制 Raw" />
            <div className="detail-json">
              <JsonTree value={event} />
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** 工具事件级视图：Overview / Input(args) / Output(result) / Raw(raw_call/raw_result)
 *  四段标签条（PRD §8.4 统一）；默认 Output（运行中的工具回退 Overview）。 */
type IoTab = 'overview' | 'input' | 'output' | 'raw';

export function ToolEventSections({ tool }: { tool: ToolCall }) {
  const argsJson = JSON.stringify(tool.args, null, 2);
  const outputText = stringifyForDisplay(tool.result);
  const resultIsObject = typeof tool.result === 'object' && tool.result !== null;
  const hasOutput = tool.result !== undefined;
  const hasRaw = Boolean(tool.raw_call || tool.raw_result);
  const [tab, setTab] = useState<IoTab>(hasOutput ? 'output' : 'overview');

  const tabs: readonly { id: IoTab; label: string }[] = [
    { id: 'overview', label: 'Overview' },
    { id: 'input', label: 'Input' },
    ...(hasOutput ? [{ id: 'output', label: 'Output' } as const] : []),
    ...(hasRaw ? [{ id: 'raw', label: 'Raw' } as const] : []),
  ];

  return (
    <div className="detail-section">
      <div className="detail-section-title">
        <TerminalSquare size={12} /> {tool.name}
        <span className={`tool-status-dot tool-status-dot-${tool.status}`} />
        {tool.started_at && tool.completed_at && (
          <span className="io-duration">{formatDuration(tool.started_at, tool.completed_at)}</span>
        )}
      </div>

      <div className="io-tabs" role="tablist" aria-label="工具输入输出">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            role="tab"
            aria-selected={tab === t.id}
            className={`io-tab${tab === t.id ? ' sel' : ''}`}
            onClick={() => setTab(t.id)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'overview' && (
        <div className="detail-overview">
          <div className="detail-row">
            <span className="detail-key">tool</span>
            <span className="detail-val detail-val-mono">{tool.name}</span>
          </div>
          <div className="detail-row">
            <span className="detail-key">tool_call_id</span>
            <span className="detail-val detail-val-mono">{tool.tool_call_id.slice(0, 16)}</span>
          </div>
          <div className="detail-row">
            <span className="detail-key">status</span>
            <span className="detail-val detail-val-tag">{tool.status}</span>
          </div>
          {tool.started_at && (
            <div className="detail-row">
              <span className="detail-key">started</span>
              <span className="detail-val detail-val-mono">{tool.started_at}</span>
            </div>
          )}
          {tool.started_at && tool.completed_at && (
            <div className="detail-row">
              <span className="detail-key">耗时</span>
              <span className="detail-val num">{formatDuration(tool.started_at, tool.completed_at)}</span>
            </div>
          )}
          {tool.artifact && (
            <div className="detail-row">
              <span className="detail-key">artifact</span>
              <span className="detail-val detail-val-mono">{tool.artifact.artifact_id.slice(0, 16)}</span>
            </div>
          )}
        </div>
      )}
      {tab === 'input' && (
        <div className="detail-code-wrap">
          <CopyButton text={argsJson} label="复制 JSON" />
          <div className="detail-json">
            <JsonTree value={tool.args} />
          </div>
        </div>
      )}
      {tab === 'output' && hasOutput && (
        <div className="detail-code-wrap">
          <CopyButton text={outputText} label="复制输出" />
          {resultIsObject ? (
            <div className="detail-json">
              <JsonTree value={tool.result} />
            </div>
          ) : (
            <pre className="detail-code">{truncateForDisplay(outputText)}</pre>
          )}
        </div>
      )}
      {tab === 'raw' && hasRaw && (
        <div className="io-raw-panes">
          {tool.raw_call && (
            <div className="detail-code-wrap">
              <div className="io-raw-label">tool/call 原始事件</div>
              <CopyButton text={JSON.stringify(tool.raw_call, null, 2)} label="复制 Raw" />
              <div className="detail-json">
                <JsonTree value={tool.raw_call} />
              </div>
            </div>
          )}
          {tool.raw_result && (
            <div className="detail-code-wrap">
              <div className="io-raw-label">tool/result 原始事件</div>
              <CopyButton text={JSON.stringify(tool.raw_result, null, 2)} label="复制 Raw" />
              <div className="detail-json">
                <JsonTree value={tool.raw_result} />
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function TabEmpty({ hint }: { hint: string }) {
  return <div className="detail-empty-hint detail-tab-empty">{hint}</div>;
}

function DetailEmpty() {
  return (
    <div className="detail-empty">
      <div className="detail-empty-title">未选择会话</div>
      <div className="detail-empty-hint">从左侧选择，或开始新任务。</div>
    </div>
  );
}

/** Format byte count as human-readable (KB / MB). */
function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
