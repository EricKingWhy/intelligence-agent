/** StepDetail — right rail showing metadata for the selected step.
 *
 * Shows: session summary, tool list + focus panel, context compaction history,
 * reconcile queue, artifact refs on overflowed tools, and a reserved checkpoint slot.
 * Data comes from ConversationState projections (invariant #22: no second truth).
 */

import { AlertTriangle, ChevronRight, Clock, Database, FileCheck2, Hash, Layers, Package } from 'lucide-react';
import type { ConversationState, ToolCall } from '../types';
import { formatDuration } from '../lib/format';
import { deriveRunPulse } from '../lib/runState';

interface Props {
  conversation: ConversationState | null;
  streaming: boolean;
  selectedTool: ToolCall | null;
  onSelectTool: (tool: ToolCall | null) => void;
}

export function StepDetail({ conversation, streaming, selectedTool, onSelectTool }: Props) {
  if (!conversation) {
    return (
      <aside className="step-detail">
        <DetailEmpty />
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

      <div className="detail-section">
        <div className="detail-section-title">
          <Hash size={12} /> 会话
        </div>
        <div className="detail-row">
          <span className="detail-key">id</span>
          <code className="detail-val">{conversation.session_id.slice(0, 16)}</code>
        </div>
        <div className="detail-row">
          <span className="detail-key">轮次</span>
          <span className="detail-val">{conversation.turns.length}</span>
        </div>
        <div className="detail-row">
          <span className="detail-key">工具</span>
          <span className="detail-val">{tools.length}</span>
        </div>
      </div>

      <div className="detail-section">
        <div className="detail-section-title">
          <Package size={12} /> 工具
        </div>
        {tools.length === 0 && <div className="detail-empty-hint">暂无工具调用。</div>}
        {tools.map((t) => (
          <button
            key={t.tool_call_id}
            className={`detail-tool-row ${selectedTool?.tool_call_id === t.tool_call_id ? 'sel' : ''}`}
            onClick={() => onSelectTool(t)}
          >
            <ChevronRight size={12} />
            <span className={`tool-status-dot tool-status-dot-${t.status}`} />
            <span className="detail-tool-name">{t.name}</span>
          </button>
        ))}
      </div>

      {selectedTool && (
        <div className="detail-section detail-tool-focus">
          <div className="detail-section-title">
            <Clock size={12} /> {selectedTool.name}
          </div>
          <div className="detail-subsection">
            <div className="detail-key">参数</div>
            <pre className="detail-code">{JSON.stringify(selectedTool.args, null, 2)}</pre>
          </div>
          {selectedTool.result !== undefined && (
            <div className="detail-subsection">
              <div className="detail-key">结果</div>
              <pre className="detail-code">
                {typeof selectedTool.result === 'string'
                  ? selectedTool.result
                  : JSON.stringify(selectedTool.result, null, 2)}
              </pre>
            </div>
          )}
          {selectedTool.started_at && selectedTool.completed_at && (
            <div className="detail-row">
              <span className="detail-key">耗时</span>
              <span className="detail-val">
                {formatDuration(selectedTool.started_at, selectedTool.completed_at)}
              </span>
            </div>
          )}
        </div>
      )}

      {/* Phase 5: Context compaction history (context/compacted events). */}
      {conversation.compactions.length > 0 && (
        <div className="detail-section">
          <div className="detail-section-title">
            <Layers size={12} /> Context 压缩
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

      {/* Phase 5: Reconcile queue (operation/reconcile-required events). */}
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

      {/* Phase 5: Artifacts produced by overflowed tools (artifact/created events). */}
      {(() => {
        const artifacts = tools.filter((t) => t.artifact);
        if (artifacts.length === 0) return null;
        return (
          <div className="detail-section">
            <div className="detail-section-title">
              <FileCheck2 size={12} /> Artifact
            </div>
            {artifacts.map((t) => (
              <div key={t.tool_call_id} className="detail-artifact-row">
                <div className="detail-row">
                  <span className="detail-key">来源</span>
                  <span className="detail-val">{t.name}</span>
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
          </div>
        );
      })()}

      {/* Reserved slot for future phases — empty by design. */}
      <div className="detail-section detail-reserved">
        <div className="detail-section-title">
          <Database size={12} /> Checkpoint
        </div>
        <div className="detail-empty-hint">Phase 7 — 预留</div>
      </div>
    </aside>
  );
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
