/** StepDetail — right rail Run Inspector (Phase 5, Brief "V1 Tabs").
 *
 * Five permanent tabs with explanatory empty states (never hidden, never faked):
 *   Chat      → Run-level summary: RUN / TOOLS / CONTEXT / ARTIFACTS / TRACE
 *               blocks + reserved MODEL / CHECKPOINT slots ("后端未暴露")
 *   Timeline  → every projected event, verbatim (seq · type · summary); click
 *               a row to drill into the event-level inspector
 *   Changes   → file diffs aggregated from edit/write/apply_patch ToolResults
 *   Terminal  → bash invocations aggregated (command + exit + output)
 *   Artifacts → artifact refs from artifact/created events
 *
 * Event-level inspector (Brief "Inspector Scope"): focusing an event swaps
 * the panel to Input / Output / Raw sections with a one-click return to
 * Run level (no modal — Brief "上下文 Inspector").
 */

import { useState } from 'react';
import {
  AlertTriangle, ArrowLeft, ChevronRight, Clock, Database, FileCheck2, FileDiff,
  Hash, Layers, ListTree, Package, TerminalSquare,
} from 'lucide-react';
import type { AgentEvent, ConversationState, ToolCall } from '../types';
import { EventType } from '../types';
import { formatDuration } from '../lib/format';
import { summarizeEvent } from '../lib/projection';
import { deriveRunPulse } from '../lib/runState';

/** Inspector focus: Run-level overview or a drilled-in event. */
export type InspectorFocus =
  | { kind: 'run' }
  | { kind: 'tool'; tool: ToolCall }
  | { kind: 'event'; event: AgentEvent };

type Tab = 'chat' | 'timeline' | 'changes' | 'terminal' | 'artifacts';

const TABS: readonly { id: Tab; label: string }[] = [
  { id: 'chat', label: 'Chat' },
  { id: 'timeline', label: 'Timeline' },
  { id: 'changes', label: 'Changes' },
  { id: 'terminal', label: 'Terminal' },
  { id: 'artifacts', label: 'Artifacts' },
];

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  focus: InspectorFocus;
  onFocusRun: () => void;
  onFocusTool: (tool: ToolCall) => void;
  onFocusEvent: (event: AgentEvent) => void;
}

export function StepDetail({ conversation, streaming, focus, onFocusRun, onFocusTool, onFocusEvent }: Props) {
  const [tab, setTab] = useState<Tab>('chat');

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
            <ArrowLeft size={12} /> 返回 Run 级
          </button>
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
      {tab === 'timeline' && <TimelineTab conversation={conversation} onFocusEvent={onFocusEvent} />}
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

      {/* 可扩展空槽（冻结决策：标注"后端未暴露"，绝不伪造） */}
      <div className="detail-section detail-reserved">
        <div className="detail-section-title">
          <Database size={12} /> MODEL
        </div>
        <div className="detail-empty-hint">后端未暴露（无事件无 API）</div>
      </div>
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

function TimelineTab({ conversation, onFocusEvent }: { conversation: ConversationState; onFocusEvent: (e: AgentEvent) => void }) {
  if (conversation.events.length === 0) {
    return <TabEmpty hint="本会话尚无事件。" />;
  }
  return (
    <div className="detail-timeline">
      {conversation.events.map((e, i) => (
        <button key={i} className="timeline-row" onClick={() => onFocusEvent(e)}>
          <span className="tl-seq">{e.seq ?? '·'}</span>
          <span className="tl-type">{e.type}</span>
          <span className="tl-summary">{eventSummary(e)}</span>
        </button>
      ))}
    </div>
  );
}

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
            {result?.stdout !== undefined && <pre className="detail-terminal-out">{result.stdout}</pre>}
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

// ── 事件级 Inspector：Input / Output / Raw（Brief "Inspector Scope"） ──

/** 事件级 Inspector 主体——只接收已收窄的非 Run 焦点。 */
type EventFocus = Exclude<InspectorFocus, { kind: 'run' }>;

function EventInspector({ focus }: { focus: EventFocus }) {
  if (focus.kind === 'tool') {
    return <ToolEventSections tool={focus.tool} />;
  }
  const event = focus.event;
  return (
    <>
      <div className="detail-section">
        <div className="detail-section-title">
          <Clock size={12} /> {event.type}
        </div>
        {event.seq !== null && (
          <div className="detail-row">
            <span className="detail-key">seq</span>
            <span className="detail-val detail-val-mono">{event.seq}</span>
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
            <span className="detail-val">{event.step_id}</span>
          </div>
        )}
      </div>
      <div className="detail-section">
        <div className="detail-section-title">Input / Output (data)</div>
        <pre className="detail-code">{JSON.stringify(event.data, null, 2)}</pre>
      </div>
      <div className="detail-section">
        <div className="detail-section-title">Raw</div>
        <pre className="detail-code">{JSON.stringify(event, null, 2)}</pre>
      </div>
    </>
  );
}

/** 工具事件级视图：Input(args) / Output(result) / Raw(raw_call/raw_result)。 */
function ToolEventSections({ tool }: { tool: ToolCall }) {
  return (
    <>
      <div className="detail-section">
        <div className="detail-section-title">
          <TerminalSquare size={12} /> {tool.name}
          <span className={`tool-status-dot tool-status-dot-${tool.status}`} />
        </div>
        <div className="detail-subsection">
          <div className="detail-key">Input (args)</div>
          <pre className="detail-code">{JSON.stringify(tool.args, null, 2)}</pre>
        </div>
        {tool.result !== undefined && (
          <div className="detail-subsection">
            <div className="detail-key">Output</div>
            <pre className="detail-code">
              {typeof tool.result === 'string' ? tool.result : JSON.stringify(tool.result, null, 2)}
            </pre>
          </div>
        )}
        {tool.started_at && tool.completed_at && (
          <div className="detail-row">
            <span className="detail-key">耗时</span>
            <span className="detail-val">{formatDuration(tool.started_at, tool.completed_at)}</span>
          </div>
        )}
      </div>
      {(tool.raw_call || tool.raw_result) && (
        <div className="detail-section">
          <div className="detail-section-title">Raw</div>
          {tool.raw_call && <pre className="detail-code">{JSON.stringify(tool.raw_call, null, 2)}</pre>}
          {tool.raw_result && <pre className="detail-code">{JSON.stringify(tool.raw_result, null, 2)}</pre>}
        </div>
      )}
    </>
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
