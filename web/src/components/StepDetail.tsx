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
        <span className="panel-label">Step Detail</span>
        <span className={`run-badge run-badge-${run}`}>{run}</span>
      </div>

      <div className="detail-section">
        <div className="detail-section-title">
          <Hash size={12} /> Session
        </div>
        <div className="detail-row">
          <span className="detail-key">id</span>
          <code className="detail-val">{conversation.session_id.slice(0, 16)}</code>
        </div>
        <div className="detail-row">
          <span className="detail-key">turns</span>
          <span className="detail-val">{conversation.turns.length}</span>
        </div>
        <div className="detail-row">
          <span className="detail-key">tools</span>
          <span className="detail-val">{tools.length}</span>
        </div>
      </div>

      <div className="detail-section">
        <div className="detail-section-title">
          <Package size={12} /> Tools
        </div>
        {tools.length === 0 && <div className="detail-empty-hint">No tool calls yet.</div>}
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
            <div className="detail-key">args</div>
            <pre className="detail-code">{JSON.stringify(selectedTool.args, null, 2)}</pre>
          </div>
          {selectedTool.result !== undefined && (
            <div className="detail-subsection">
              <div className="detail-key">result</div>
              <pre className="detail-code">
                {typeof selectedTool.result === 'string'
                  ? selectedTool.result
                  : JSON.stringify(selectedTool.result, null, 2)}
              </pre>
            </div>
          )}
          {selectedTool.started_at && selectedTool.completed_at && (
            <div className="detail-row">
              <span className="detail-key">duration</span>
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
        <div className="detail-empty-hint">Phase 7 — reserved</div>
      </div>
      <div className="detail-section detail-reserved">
        <div className="detail-section-title">Artifact</div>
        <div className="detail-empty-hint">Phase 6 — reserved</div>
      </div>
    </aside>
  );
}

function DetailEmpty() {
  return (
    <div className="detail-empty">
      <div className="detail-empty-title">No session selected</div>
      <div className="detail-empty-hint">Pick one from the left, or start a new task.</div>
    </div>
  );
}
