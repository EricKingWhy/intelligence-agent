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
import { useVirtualizer } from '@tanstack/react-virtual';
import { Activity } from 'lucide-react';
import type { ChainNode } from '../lib/projection';
import { deriveChain } from '../lib/projection';
import type { TraceDensity } from '../lib/density';
import type { Disclosure } from '../lib/disclosure';
import { nextLevel, toolEventKey } from '../lib/disclosure';
import { KIND_ICON, KIND_LABEL, modelKind, type RuntimeEventKind } from '../lib/eventKind';
import type { ConversationState, ModelSegment, ToolCall, Turn } from '../types';
import { formatDuration, truncateForDisplay } from '../lib/format';
import { renderMarkdown } from '../lib/markdown';
import { ToolCard } from './ToolCard';
import { CopyButton } from './CopyButton';

interface Props {
  conversation: ConversationState | null;
  loadingHistory: boolean;
  /** Trace Density 四档（Brief 冻结决策）——控制执行链节点粒度。 */
  density: TraceDensity;
  /** L0-L2 展开状态（manual override ?? density 默认）。缺省 = 无手动层。 */
  disclosure?: Disclosure;
  /** Inspector → 主区反向联动（PRD §9.2）：定位目标 key + 变更序号（nonce 保证
   *  重复跳同一目标也触发 effect）。 */
  jumpRequest?: { key: string; nonce: number } | null;
  /** 空状态示例任务回调——点击 chip 时由 App 注入 Composer。 */
  onPresetTask?: (text: string) => void;
  /** hover Inspect → 钻取到事件级 Inspector（PRD §9.1 联动）。 */
  onFocusTool?: (tool: ToolCall) => void;
}

const EMPTY_TURNS: Turn[] = [];

const EXAMPLE_TASKS = [
  '写一个 FizzBuzz 脚本并运行验证',
  '创建 todo.md，写入三条今日计划',
  '列出当前目录的文件结构并总结',
];

export function Conversation({ conversation, loadingHistory, density, disclosure, jumpRequest, onPresetTask, onFocusTool }: Props) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const endRef = useRef<HTMLDivElement>(null);

  // PRD §20.2 / ADR-0014 D7：turns 列表窗口化（@tanstack/react-virtual）。
  // turn 是虚拟单元（user 消息 + 执行链，高度差异大）→ measureElement 动态测高；
  // overscan 6 保证滚动无白边；流式活跃 turn 高度连续变化由动态测量吸收。
  // 少 turn（<15）时与全渲染等价，统一路径避免双渲染逻辑。
  const turns = conversation?.turns ?? EMPTY_TURNS;
  const virtualizer = useVirtualizer({
    count: turns.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => 240,
    overscan: 6,
    getItemKey: (i) => turns[i].step_id,
  });

  // PRD §9.2 反向联动：Inspector Timeline 点行 → 中间滚动定位 + 短促 pulse。
  // 目标可能是工具行（data-stream-key）或轮次容器（data-step-key）。
  // 虚拟化后目标行可能不在已挂载窗口内：先定位归属 turn，scrollToIndex 渲染
  // 出来后再 pulse（requestAnimationFrame 等一帧 DOM 提交）。
  // nonce 守卫：turns/virtualizer 在依赖里（窗口外兜底需要最新值），流式期间
  // 每 delta 都会变——已处理的 jumpRequest 不得重复触发 pulse。
  const processedJumpNonce = useRef<number | null>(null);
  useEffect(() => {
    if (!jumpRequest || processedJumpNonce.current === jumpRequest.nonce) return;
    processedJumpNonce.current = jumpRequest.nonce;
    const root = scrollRef.current;
    if (!root) return;
    const pulse = (el: HTMLElement) => {
      el.classList.remove('stream-jump-pulse');
      void el.offsetWidth; // 强制 reflow：同目标重复跳转也重启动画
      el.classList.add('stream-jump-pulse');
      window.setTimeout(() => el.classList.remove('stream-jump-pulse'), 900);
    };
    const el =
      root.querySelector<HTMLElement>(`[data-stream-key="${jumpRequest.key}"]`) ??
      root.querySelector<HTMLElement>(`[data-step-key="${jumpRequest.key}"]`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      pulse(el);
      return;
    }
    // 窗口外兜底：key → turn index → scrollToIndex
    const idx = turns.findIndex((t) =>
      jumpRequest.key.startsWith('tool:')
        ? t.tools.some((x) => `tool:${x.tool_call_id}` === jumpRequest.key)
        : `step:${t.step_id}` === jumpRequest.key,
    );
    if (idx === -1) return;
    virtualizer.scrollToIndex(idx, { align: 'center' });
    requestAnimationFrame(() => {
      const el2 =
        root.querySelector<HTMLElement>(`[data-stream-key="${jumpRequest.key}"]`) ??
        root.querySelector<HTMLElement>(`[data-step-key="${jumpRequest.key}"]`);
      if (el2) pulse(el2);
    });
  }, [jumpRequest, turns, virtualizer]);

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
        {/* 虚拟化：绝对定位行 + 动态测高（PRD §20.2）。只有窗口内 turn 参与 DOM。 */}
        <div style={{ height: virtualizer.getTotalSize(), position: 'relative' }}>
          {virtualizer.getVirtualItems().map((vi) => (
            <div
              key={vi.key}
              data-index={vi.index}
              ref={virtualizer.measureElement}
              style={{
                position: 'absolute',
                top: 0,
                left: 0,
                width: '100%',
                transform: `translateY(${vi.start}px)`,
              }}
            >
              <TurnView
                turn={turns[vi.index]}
                model={conversation.model}
                density={density}
                disclosure={disclosure}
                onFocusTool={onFocusTool}
              />
            </div>
          ))}
        </div>
        <div ref={endRef} />
      </div>
    </div>
  );
}

// memo + 投影层 copy-on-write（未触及 turn 引用稳定）：流式期间每个 delta 只
// 重渲染活跃轮次——已完成轮次不再重跑 deriveChain 与全量 markdown 重解析。
const TurnView = memo(function TurnView({ turn, model, density, disclosure, onFocusTool }: { turn: Turn; model: string | null; density: TraceDensity; disclosure?: Disclosure; onFocusTool?: (tool: ToolCall) => void }) {
  // 折叠是纯手动选项（用户指令 2026-09-05，覆盖冻结决策 L48 的"默认折叠"）：
  // 完成轮一律默认展开——先让用户看到模型回答，想收起再手动点。live 与
  // 历史重挂载行为一致；流式中/无模型文本的轮次不出现折叠按钮。
  const collapsible = turn.status !== 'streaming' && turn.model.text.length > 0;
  const [collapsed, setCollapsed] = useState(false);

  const duration = formatDuration(turn.started_at, turn.completed_at);
  const chain = useMemo(() => deriveChain(turn), [turn]);
  // D12/PRD §10：chain 中最后一个 model 节点 = Final Answer（高对比正文），
  // 其余 done model 段是中间输出（低对比 + 语义行）。
  const lastModelIndex = useMemo(() => {
    for (let i = chain.length - 1; i >= 0; i--) {
      if (chain[i].kind === 'model') return i;
    }
    return -1;
  }, [chain]);
  // hover 时间戳（调研：完成后才展示，流式期间不打扰；title 属性最轻实现）
  const completedTitle = turn.completed_at
    ? `完成于 ${new Date(turn.completed_at).toLocaleString()}`
    : undefined;

  return (
    <div className={`turn turn-${turn.status}`} data-step-key={`step:${turn.step_id}`}>
      {/* User message — minimal, right-aligned */}
      {turn.user_message && (
        <div className="msg msg-user">
          <div className="msg-bubble-user">{turn.user_message}</div>
        </div>
      )}

      {/* Execution chain — model segments and tools in true event order */}
      {turn.activities.length > 0 && (
        <div className="msg msg-model" title={completedTitle}>
          <div className="msg-body">
            {/* 工具行：折叠按钮（手动选项）+ 模型名小标签（调研 pitfall #6：
                每条 AI 消息标注模型名，升级/降级模型时一眼可辨） */}
            {(collapsible || (model && turn.model.text)) && (
              <div className="turn-tools-row">
                {collapsible && (
                  <button className="turn-collapse-btn" onClick={() => setCollapsed((v) => !v)}>
                    {collapsed
                      ? `已折叠 · ${turn.tools.length} 个工具 · ${turn.segments.length} 轮${duration ? ` · ${duration}` : ''}`
                      : '折叠'}
                  </button>
                )}
                {model && turn.model.text && (
                  <span className="model-tag" title="本次运行使用的模型">{model}</span>
                )}
              </div>
            )}
            {!collapsed && (
              <div className="act-chain">
                {chain.map((node, i) => (
                  <ChainNodeView
                    key={chainKey(node, i)}
                    node={node}
                    density={density}
                    disclosure={disclosure}
                    isFinalModel={i === lastModelIndex}
                    onFocusTool={onFocusTool}
                  />
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

export function ChainNodeView({ node, density, disclosure, isFinalModel = true, onFocusTool }: { node: ChainNode; density: TraceDensity; disclosure?: Disclosure; /** 该 model 段是否为 turn 最后一个模型段（final-answer 高对比）。 */ isFinalModel?: boolean; onFocusTool?: (tool: ToolCall) => void }) {
  if (node.kind === 'tool') {
    const key = toolEventKey(node.tool.tool_call_id);
    const cycle = disclosure
      ? () => disclosure.setLevel(key, nextLevel(disclosure.levelFor(key, density)))
      : undefined;
    return (
      <ToolCard
        tool={node.tool}
        density={density}
        level={disclosure ? disclosure.levelFor(key, density) : undefined}
        onCycleLevel={cycle}
        onFocus={onFocusTool}
      />
    );
  }
  const { segment }: { segment: ModelSegment } = node;
  const kind: RuntimeEventKind = modelKind(segment.status, isFinalModel);
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
  const display = truncateForDisplay(segment.text);
  // 语义图标行（PRD §5.2/§10）：思考中（streaming）/ 中间输出（done 非终段）。
  // final-answer 不出行（高对比正文直接呈现，PRD §11.2）。
  const kindRow =
    kind === 'final-answer' ? null : (
      <div className={`model-kind-row model-kind-row-${kind}`}>
        <span className="model-kind-icon">{(() => { const Icon = KIND_ICON[kind]; return <Icon size={13} />; })()}</span>
        <span className="model-kind-label">{KIND_LABEL[kind]}</span>
        {kind === 'thinking' && <span className="model-kind-ellipsis" aria-hidden="true" />}
      </div>
    );
  // P0-2a 流式 markdown 增量化（HANDOFF §6）：streaming 段渲染纯文本
  // （pre-wrap 样式保留换行，标记原样透传——打字机状态本就不需要排版），
  // model/completed 置 done 后一次性 renderMarkdown。消灭流式期间每 delta
  // 全量重解析的 CPU 开销。纯文本走 React 文本节点，天然零 XSS 面。
  if (segment.status === 'streaming') {
    return (
      <div className="model-output-wrap">
        {kindRow}
        <div className="model-output streaming">
          {display}
          <span className="stream-caret" />
        </div>
      </div>
    );
  }
  // hover 复制（调研：per-message copy 是 AI chat 标配动作；仅完成段提供，
  // 流式段文本还在增长，复制半成品是噪音）。中间段低对比（D17 分层），
  // final-answer 保持正文高对比。
  return (
    <div className="model-output-wrap">
      {kindRow}
      <div className={`model-output ${segment.status}${kind === 'model' ? ' model-output-intermediate' : ''}`}>
        {renderMarkdown(display)}
      </div>
      {segment.text && <CopyButton text={segment.text} label={kind === 'final-answer' ? '复制回答' : '复制输出'} />}
    </div>
  );
}
