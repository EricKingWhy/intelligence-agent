/** Conversation — center column rendering the projected ConversationState.
 *
 * Each Turn renders as (Trace Ladder, signature #2 — Brief §24 Phase 3):
 *   - user message (right-aligned bubble)
 *   - execution chain in TRUE event order: model segments ↔ ToolCards,
 *     projected by deriveChain (shared projection.ts layer — no second truth)
 *
 * Turn collapse: completed turns with model text collapse to a derived
 * summary line (N tools · M 轮 · 真实耗时) — replaces the old fabricated
 * "~N tok" estimate (zero-fake-metrics rule).
 */

import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { Activity, Brain } from 'lucide-react';
import type { ChainNode } from '../lib/projection';
import { deriveChain } from '../lib/projection';
import type { TraceDensity } from '../lib/density';
import type { ConversationState, ModelSegment, ToolCall, Turn } from '../types';
import { formatDuration, truncateForDisplay } from '../lib/format';
import { renderMarkdown } from '../lib/markdown';
import { ToolCard } from './ToolCard';

interface Props {
  conversation: ConversationState | null;
  loadingHistory: boolean;
  /** Trace Density 四档（Brief 冻结决策）——控制执行链节点粒度。 */
  density: TraceDensity;
  /** 空状态示例任务回调——点击 chip 时由 App 注入 Composer。 */
  onPresetTask?: (text: string) => void;
  /** 点击工具块钻取到事件级 Inspector（Brief "Inspector Scope"）。 */
  onFocusTool?: (tool: ToolCall) => void;
}

const EXAMPLE_TASKS = [
  '写一个 FizzBuzz 脚本并运行验证',
  '创建 todo.md，写入三条今日计划',
  '列出当前目录的文件结构并总结',
];

export function Conversation({ conversation, loadingHistory, density, onPresetTask, onFocusTool }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // Auto-scroll while streaming — but only when the user is already near the
  // bottom; scrolling up to read history must not be yanked back every delta.
  useEffect(() => {
    if (conversation?.run_status !== 'running') return;
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) {
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
    <div className="conversation">
      {/* key 换 session 时整组 turn remount，触发 fade-in = 切换 crossfade 感 */}
      <div className="conversation-scroll" key={conversation.session_id} ref={scrollRef}>
        {conversation.turns.map((turn) => (
          <TurnView
            key={turn.step_id}
            turn={turn}
            density={density}
            onFocusTool={onFocusTool}
          />
        ))}
        <div ref={endRef} />
      </div>
    </div>
  );
}

// memo + 投影层 copy-on-write（未触及 turn 引用稳定）：流式期间每个 delta 只
// 重渲染活跃轮次——已完成轮次不再重跑 deriveChain 与全量 markdown 重解析。
const TurnView = memo(function TurnView({ turn, density, onFocusTool }: { turn: Turn; density: TraceDensity; onFocusTool?: (tool: ToolCall) => void }) {
  // 折叠是纯手动选项（用户指令 2026-09-05，覆盖冻结决策 L48 的"默认折叠"）：
  // 完成轮一律默认展开——先让用户看到模型回答，想收起再手动点。live 与
  // 历史重挂载行为一致；流式中/无模型文本的轮次不出现折叠按钮。
  const collapsible = turn.status !== 'streaming' && turn.model.text.length > 0;
  const [collapsed, setCollapsed] = useState(false);

  const duration = formatDuration(turn.started_at, turn.completed_at);
  const chain = useMemo(() => deriveChain(turn), [turn]);

  return (
    <div className={`turn turn-${turn.status}`}>
      {/* User message — minimal, right-aligned */}
      {turn.user_message && (
        <div className="msg msg-user">
          <div className="msg-bubble-user">{turn.user_message}</div>
        </div>
      )}

      {/* Execution chain — model segments and tools in true event order */}
      {turn.activities.length > 0 && (
        <div className="msg msg-model">
          <div className="msg-avatar msg-avatar-model"><Brain size={13} /></div>
          <div className="msg-body">
            {/* 折叠摘要：派生计数 + 真实耗时，零伪造指标 */}
            {collapsible && (
              <button className="turn-collapse-btn" onClick={() => setCollapsed((v) => !v)}>
                {collapsed
                  ? `已折叠 · ${turn.tools.length} 个工具 · ${turn.segments.length} 轮${duration ? ` · ${duration}` : ''}`
                  : '折叠'}
              </button>
            )}
            {!collapsed && (
              <div className="act-chain">
                {chain.map((node, i) => (
                  <ChainNodeView key={chainKey(node, i)} node={node} density={density} onFocusTool={onFocusTool} />
                ))}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
});

function chainKey(node: ChainNode, i: number): string {
  return node.kind === 'tool' ? node.tool.tool_call_id : `model-${i}`;
}

function ChainNodeView({ node, density, onFocusTool }: { node: ChainNode; density: TraceDensity; onFocusTool?: (tool: ToolCall) => void }) {
  if (node.kind === 'tool') {
    return <ToolCard tool={node.tool} density={density} onFocus={onFocusTool} />;
  }
  const { segment }: { segment: ModelSegment } = node;
  // Compact 档下 done 的 model 段只渲染首行摘要（渐进披露：详情留给 Inspector）
  if (density === 'compact' && segment.status !== 'streaming') {
    const first = segment.text.split('\n').find((l) => l.trim()) ?? '';
    if (!first) return null;
    return (
      <div className="model-output done model-output-compact">{renderMarkdown(truncateForDisplay(first))}</div>
    );
  }
  if (!segment.text && segment.status !== 'streaming') return null;
  // 模型文本与工具输出同级不可信——单行超长模型输出同样会冻结 UI，渲染前截断
  // （41e7360 只覆盖了工具路径，code-review 补齐此处）。
  return (
    <div className={`model-output ${segment.status}`}>
      {renderMarkdown(truncateForDisplay(segment.text))}
      {segment.status === 'streaming' && <span className="stream-caret" />}
    </div>
  );
}
