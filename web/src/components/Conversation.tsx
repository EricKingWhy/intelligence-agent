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
import { Activity, Brain } from 'lucide-react';
import type { ConversationState, Turn } from '../types';
import { renderMarkdown } from '../lib/markdown';
import { ToolCard } from './ToolCard';

interface Props {
  conversation: ConversationState | null;
  loadingHistory: boolean;
  /** 空状态示例任务回调——点击 chip 时由 App 注入 Composer。 */
  onPresetTask?: (text: string) => void;
}

const EXAMPLE_TASKS = [
  '写一个 FizzBuzz 脚本并运行验证',
  '创建 todo.md，写入三条今日计划',
  '列出当前目录的文件结构并总结',
];

export function Conversation({ conversation, loadingHistory, onPresetTask }: Props) {
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
        <div className="conversation-empty">正在加载历史…</div>
      </div>
    );
  }

  if (!conversation || conversation.turns.length === 0) {
    return (
      <div className="conversation">
        <div className="conversation-empty">
          <div className="empty-logo"><Activity size={20} /></div>
          <div className="empty-hero">暂无对话</div>
          <div className="empty-sub">在下方提交任务，实时观看 Agent 执行。</div>
          {onPresetTask && (
            <div className="empty-examples">
              {EXAMPLE_TASKS.map((task) => (
                <button key={task} className="example-chip" onClick={() => onPresetTask(task)}>
                  {task}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="conversation" ref={scrollRef}>
      {/* key 换 session 时整组 turn remount，触发 fade-in = 切换 crossfade 感 */}
      <div className="conversation-scroll" key={conversation.session_id}>
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
  const hasText = turn.model.text.length > 0;
  const canCollapse = turn.status !== 'streaming' && (hasText || turn.tools.length > 0);
  const [collapsed, setCollapsed] = useState(false);

  // Active turn always expanded.
  useEffect(() => {
    if (active && turn.status === 'streaming') setCollapsed(false);
  }, [active, turn.status]);

  const tokenCount = Math.max(1, Math.ceil(turn.model.text.length / 4));

  return (
    <div className={`turn turn-${turn.status}`}>
      {/* User message — minimal, right-aligned */}
      {turn.user_message && (
        <div className="msg msg-user">
          <div className="msg-bubble-user">{turn.user_message}</div>
        </div>
      )}

      {/* Model + tools — activity timeline */}
      {(turn.model.text || turn.tools.length > 0) && (
        <div className="msg msg-model">
          <div className="msg-avatar msg-avatar-model"><Brain size={13} /></div>
          <div className="msg-body">
            {/* 纯工具轮次不显示折叠控件——工具节点本身已极简，按钮只会制造噪音 */}
            {canCollapse && hasText && (
              <button className="turn-collapse-btn" onClick={() => setCollapsed((v) => !v)}>
                {collapsed
                  ? `思考 · ${turn.tools.length} 个工具 · ~${tokenCount} tok`
                  : '折叠'}
              </button>
            )}
            {!collapsed && (
              <>
                {turn.model.text && (
                  <div className={`model-output ${turn.model.status}`}>
                    {renderMarkdown(turn.model.text)}
                    {turn.model.status === 'streaming' && <span className="stream-caret" />}
                  </div>
                )}
                {turn.tools.length > 0 && (
                  <div className="act-stream">
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
