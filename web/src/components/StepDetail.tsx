/** StepDetail — right rail showing metadata for the selected step.
 *
 * V1 shows: model request meta, tool args/result, retry/duration, checkpoint &
 * artifact as empty slots (reserved for future phases).
 */

import { ChevronRight, Clock, Hash, Package } from 'lucide-react';
import type { ConversationState, ToolCall } from '../types';

interface Props {
  conversation: ConversationState | null;
  selectedTool: ToolCall | null;
  onSelectTool: (tool: ToolCall | null) => void;
}

const RUN_LABELS: Record<ConversationState['run_status'], string> = {
  idle: '空闲',
  running: '运行中',
  completed: '已完成',
  failed: '失败',
};

export function StepDetail({ conversation, selectedTool, onSelectTool }: Props) {
  if (!conversation) {
    return (
      <aside className="step-detail">
        <DetailEmpty />
      </aside>
    );
  }

  const tools = conversation.turns.flatMap((t) => t.tools);
  const run = conversation.run_status;

  return (
    <aside className="step-detail">
      <div className="detail-header">
        <span className="panel-label">步骤详情</span>
        <span className={`run-badge run-badge-${run}`}>{RUN_LABELS[run]}</span>
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
                {(
                  new Date(selectedTool.completed_at).getTime() -
                  new Date(selectedTool.started_at).getTime()
                )}
                ms
              </span>
            </div>
          )}
        </div>
      )}

      {/* Reserved slots for future phases — empty by design. */}
      <div className="detail-section detail-reserved">
        <div className="detail-section-title">Checkpoint</div>
        <div className="detail-empty-hint">Phase 7 — 预留</div>
      </div>
      <div className="detail-section detail-reserved">
        <div className="detail-section-title">Artifact</div>
        <div className="detail-empty-hint">Phase 6 — 预留</div>
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
