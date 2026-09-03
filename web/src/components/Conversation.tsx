/** Conversation — center column rendering the projected ConversationState.
 *
 * Each Turn renders as:
 *   - user message (right-aligned bubble)
 *   - model output (left-aligned, streamed via model/delta)
 *   - tool calls (ToolCards, collapsible)
 *
 * Turn collapse: completed turns collapse to a summary line ("3 tools · 12 tokens").
 * The active streaming turn is always expanded.
 */

import { useEffect, useRef, useState } from 'react';
import { Brain, User } from 'lucide-react';
import type { ConversationState, Turn } from '../types';
import { ToolCard } from './ToolCard';

interface Props {
  conversation: ConversationState | null;
  loadingHistory: boolean;
}

export function Conversation({ conversation, loadingHistory }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom while streaming.
  useEffect(() => {
    if (conversation?.run_status === 'running') {
      endRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }
  }, [conversation]);

  if (loadingHistory) {
    return (
      <div className="conversation">
        <div className="conversation-empty">Loading history…</div>
      </div>
    );
  }

  if (!conversation || conversation.turns.length === 0) {
    return (
      <div className="conversation">
        <div className="conversation-empty">
          <div className="empty-hero">No conversation yet</div>
          <div className="empty-sub">Submit a task below to watch the agent work in real time.</div>
        </div>
      </div>
    );
  }

  return (
    <div className="conversation" ref={scrollRef}>
      <div className="conversation-scroll">
        {conversation.turns.map((turn) => (
          <TurnView key={turn.step_id} turn={turn} active={conversation.run_status === 'running'} />
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}

function TurnView({ turn, active }: { turn: Turn; active: boolean }) {
  // A turn is collapsible only if it's done and has content + tools.
  const canCollapse = turn.status !== 'streaming' && (turn.model.text.length > 0 || turn.tools.length > 0);
  const [collapsed, setCollapsed] = useState(false);

  // Active turn always expanded.
  useEffect(() => {
    if (active && turn.status === 'streaming') setCollapsed(false);
  }, [active, turn.status]);

  const tokenCount = Math.max(1, Math.ceil(turn.model.text.length / 4));

  return (
    <div className={`turn turn-${turn.status}`}>
      {/* User message */}
      {turn.user_message && (
        <div className="msg msg-user">
          <div className="msg-avatar"><User size={14} /></div>
          <div className="msg-bubble msg-bubble-user">{turn.user_message}</div>
        </div>
      )}

      {/* Model + tools */}
      {(turn.model.text || turn.tools.length > 0) && (
        <div className="msg msg-model">
          <div className="msg-avatar msg-avatar-model"><Brain size={14} /></div>
          <div className="msg-body">
            {canCollapse && (
              <button className="turn-collapse-btn" onClick={() => setCollapsed((v) => !v)}>
                {collapsed
                  ? `Thought · ${turn.tools.length} tools · ~${tokenCount} tok`
                  : 'collapse'}
              </button>
            )}
            {!collapsed && (
              <>
                {turn.model.text && (
                  <div className={`model-output ${turn.model.status}`}>
                    {turn.model.text}
                    {turn.model.status === 'streaming' && <span className="stream-caret" />}
                  </div>
                )}
                {turn.tools.length > 0 && (
                  <div className="turn-tools">
                    {turn.tools.map((t) => (
                      <ToolCard key={t.tool_call_id} tool={t} />
                    ))}
                  </div>
                )}
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
