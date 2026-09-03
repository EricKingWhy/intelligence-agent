/** ToolCard — per-tool renderer with specialized layouts.
 *
 * Three shapes:
 *   - bash → terminal block (mono, dark inset, exit code badge)
 *   - edit/apply_patch/write → diff view (green/red, before/after)
 *   - everything else → collapsible generic card (name · args · result)
 *
 * Status color: running=warning, success=green, failed=red.
 */

import { useState } from 'react';
import { Check, ChevronRight, ChevronDown, Terminal, Wrench, X } from 'lucide-react';
import type { ToolCall } from '../types';

interface Props {
  tool: ToolCall;
}

export function ToolCard({ tool }: Props) {
  const isBash = tool.name === 'bash';
  const isDiffTool = ['edit', 'apply_patch', 'write'].includes(tool.name);
  const [expanded, setExpanded] = useState(false);

  const statusClass = `tool-status tool-status-${tool.status}`;

  return (
    <div className={`tool-card ${statusClass}`}>
      <button className="tool-card-header" onClick={() => setExpanded((v) => !v)}>
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        {isBash ? <Terminal size={14} /> : <Wrench size={14} />}
        <span className="tool-name">{tool.name}</span>
        <span className="tool-args-summary">{summarizeArgs(tool)}</span>
        <span className="tool-status-badge">
          {tool.status === 'success' && <Check size={12} />}
          {tool.status === 'failed' && <X size={12} />}
          {tool.status === 'running' && <span className="status-spinner" />}
        </span>
      </button>

      {expanded && (
        <div className="tool-card-body">
          {isBash && <BashBlock tool={tool} />}
          {isDiffTool && tool.diff && <DiffBlock diff={tool.diff} />}
          {!isBash && !isDiffTool && <GenericBlock tool={tool} />}
        </div>
      )}
    </div>
  );
}

function summarizeArgs(tool: ToolCall): string {
  const a = tool.args;
  if (tool.name === 'bash') return String(a.command ?? '').slice(0, 60);
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
