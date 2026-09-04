/** ToolCard — per-tool renderer with specialized layouts.
 *
 * Four shapes (expanded):
 *   - bash → terminal block (mono, dark inset, exit code badge)
 *   - edit/apply_patch/write → diff view (green/red, before/after)
 *   - inspect_artifact → artifact slice view (numbered lines, per-line truncation chips)
 *   - everything else → collapsible generic card (name · args · result)
 *
 * Trace Density tiers (Brief — frozen decision):
 *   - compact  → status line only (✓ + name)
 *   - balanced → name + key args + duration + status (default)
 *   - detailed → balanced + full args JSON + result preview on the node itself
 *   - raw      → detailed + verbatim source-event JSON (raw_call/raw_result)
 *
 * Status color: running=warning, success=green, failed=red.
 */

import { useState } from 'react';
import { Check, Scissors, Terminal, Wrench, X } from 'lucide-react';
import type { ToolCall } from '../types';
import type { TraceDensity } from '../lib/density';
import { formatDuration } from '../lib/format';

interface Props {
  tool: ToolCall;
  density: TraceDensity;
}

export function ToolCard({ tool, density }: Props) {
  const isBash = tool.name === 'bash';
  const isDiffTool = ['edit', 'apply_patch', 'write'].includes(tool.name);
  const slice = tool.name === 'inspect_artifact' ? tryParseSlice(tool.result) : null;
  const [expanded, setExpanded] = useState(false);
  const duration = formatDuration(tool.started_at, tool.completed_at);
  const detailed = density === 'detailed' || density === 'raw';

  return (
    <>
      <button
        className={`act-node act-node-${density}`}
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        <span className={`act-status act-status-${tool.status}`}>
          {tool.status === 'success' && <Check size={12} />}
          {tool.status === 'failed' && <X size={12} />}
          {tool.status === 'running' && <span className="status-spinner" />}
        </span>
        {density !== 'compact' && <span className="act-icon">{isBash ? <Terminal size={13} /> : <Wrench size={13} />}</span>}
        <span className="act-name">{tool.name}</span>
        {density !== 'compact' && <span className="act-args">{summarizeArgs(tool)}</span>}
        {duration && <span className="act-duration">{duration}</span>}
        {density === 'compact' && (
          <span className="act-args act-args-compact">{summarizeArgs(tool, 32)}</span>
        )}
      </button>

      {/* 内联明细在 compact/balanced 不渲染；detailed/raw 展开时保留（四形态 body 追加在下方） */}
      {detailed && (
        <div className="act-detail-inline">
          <pre className="act-detail-args">{JSON.stringify(tool.args, null, 2)}</pre>
          {tool.result !== undefined && (
            <pre className="act-detail-result">
              {truncate(
                typeof tool.result === 'string' ? tool.result : JSON.stringify(tool.result, null, 2),
                400,
              )}
            </pre>
          )}
          {density === 'raw' && (tool.raw_call || tool.raw_result) && (
            <div className="act-raw">
              {tool.raw_call && (
                <div className="act-raw-section">
                  <div className="act-raw-label">tool/call 原始事件</div>
                  <pre>{JSON.stringify(tool.raw_call, null, 2)}</pre>
                </div>
              )}
              {tool.raw_result && (
                <div className="act-raw-section">
                  <div className="act-raw-label">tool/result 原始事件</div>
                  <pre>{JSON.stringify(tool.raw_result, null, 2)}</pre>
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {expanded && (
        <div className="tool-card-body">
          {isBash && <BashBlock tool={tool} />}
          {isDiffTool && tool.diff && <DiffBlock diff={tool.diff} />}
          {slice && <ArtifactSliceBlock slice={slice} />}
          {!isBash && !isDiffTool && !slice && <GenericBlock tool={tool} />}
        </div>
      )}
    </>
  );
}

function summarizeArgs(tool: ToolCall, max = 60): string {
  const a = tool.args;
  if (tool.name === 'bash') return String(a.command ?? '').slice(0, max);
  if ('path' in a) return String(a.path);
  const entries = Object.entries(a).slice(0, 2);
  return entries.map(([k, v]) => `${k}=${truncate(JSON.stringify(v), 30)}`).join(' ');
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n) + '…' : s;
}

function BashBlock({ tool }: { tool: ToolCall }) {
  const cmd = String(tool.args.command ?? '');
  const out = typeof tool.result === 'string' ? tool.result : JSON.stringify(tool.result ?? '', null, 2);
  const exitCode = (tool.result as { exit_code?: number } | string | undefined);
  const code = typeof exitCode === 'object' && exitCode ? exitCode.exit_code : undefined;

  return (
    <div className="bash-block">
      <div className="bash-cmd-line">
        <span className="bash-prompt">$</span>
        <code>{cmd}</code>
      </div>
      {out && <pre className="bash-output">{out}</pre>}
      {code !== undefined && (
        <span className={`exit-badge ${code === 0 ? 'exit-ok' : 'exit-err'}`}>exit {code}</span>
      )}
    </div>
  );
}

function DiffBlock({ diff }: { diff: NonNullable<ToolCall['diff']> }) {
  return (
    <div className="diff-block">
      {diff.truncated && <div className="diff-truncated">内容过长，已截断显示</div>}
      <div className="diff-cols">
        <div className="diff-col diff-before">
          <div className="diff-col-label">变更前</div>
          <pre>{diff.before || '（空）'}</pre>
        </div>
        <div className="diff-col diff-after">
          <div className="diff-col-label">变更后</div>
          <pre>{diff.after || '（空）'}</pre>
        </div>
      </div>
    </div>
  );
}

function GenericBlock({ tool }: { tool: ToolCall }) {
  return (
    <div className="generic-block">
      <div className="generic-section">
        <div className="generic-label">参数</div>
        <pre>{JSON.stringify(tool.args, null, 2)}</pre>
      </div>
      {tool.result !== undefined && (
        <div className="generic-section">
          <div className="generic-label">结果</div>
          <pre>{typeof tool.result === 'string' ? tool.result : JSON.stringify(tool.result, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

// ── inspect_artifact 专用渲染 ──

/** 后端 ArtifactSlice.lines 的单行 item 形状（spec 06 §4 / storage/artifact.py）。
 *  超长行截断后额外携带 truncated=true + full_length（原始字符数）。 */
interface SliceLine {
  line_number: number;
  text: string;
  truncated?: boolean;
  full_length?: number;
}

interface SliceData {
  artifact_id: string;
  lines: SliceLine[];
  total_lines: number;
  returned_lines: number;
  truncated: boolean;
}

/** 从 ToolResult 中解析 ArtifactSlice（data 字段经 tryParseContent 已是对象）。
 *  形状不符（失败结果 / 旧数据）返回 null，回退 GenericBlock。 */
function tryParseSlice(result: unknown): SliceData | null {
  if (typeof result !== 'object' || result === null) return null;
  const r = result as Record<string, unknown>;
  if (!Array.isArray(r.lines)) return null;
  const lines = r.lines.filter(
    (l): l is SliceLine =>
      typeof l === 'object' && l !== null &&
      typeof (l as Record<string, unknown>).line_number === 'number' &&
      typeof (l as Record<string, unknown>).text === 'string',
  );
  if (lines.length === 0) return null;
  return {
    artifact_id: String(r.artifact_id ?? ''),
    lines,
    total_lines: Number(r.total_lines ?? 0),
    returned_lines: Number(r.returned_lines ?? lines.length),
    truncated: r.truncated === true,
  };
}

function ArtifactSliceBlock({ slice }: { slice: SliceData }) {
  const truncatedCount = slice.lines.filter((l) => l.truncated).length;
  return (
    <div className="slice-block">
      <div className="slice-meta">
        <span className="slice-meta-item">共 {slice.total_lines} 行</span>
        {slice.truncated && <span className="slice-meta-item">返回 {slice.lines.length} 行</span>}
        {truncatedCount > 0 && (
          <span className="slice-trunc-chip">
            <Scissors size={11} />
            {truncatedCount} 行超长截断 · 可放宽 max_chars_per_line 重读
          </span>
        )}
      </div>
      <div className="slice-lines">
        {slice.lines.map((l) => (
          <div key={l.line_number} className={`slice-line ${l.truncated ? 'slice-line-trunc' : ''}`}>
            <span className="slice-line-no">{l.line_number}</span>
            <code className="slice-line-text">{l.text}</code>
            {l.truncated && (
              <span className="slice-line-chip" title={`原始 ${l.full_length ?? '?'} 字符，当前显示前 2000`}>
                …{l.full_length ?? '?'} 字符
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
