/** SubagentRoster / SubagentSplitPane — #1256 子智能体可见性（对标 dsh）。
 *
 * 两件事，一个特性模块：
 *  ① 顶栏 roster 下拉：列出当前会话的子智能体（名/token/耗时/状态点），行主体点击
 *     在主视图打开（现有 handleSelect 语义），chevron 在右侧分栏打开。
 *  ② 右侧分栏：打开子智能体的**完整会话**（复用 Conversation + useChildConversation，
 *     不建第二套渲染器），父会话保持不动。
 *
 * 设计来源（PORT DESIGN，非代码复制）：dsh `packages/client/ui-subagent/src/client/
 * SubagentHeaderLineage.tsx` 的 panel 设计——durable 记录 + live 叠加、token 缩写
 * x.xK/x.xM、无子智能体不渲染入口；openChild(主视图)/openChildAside(右栏) 双打开语义
 * （dsh `src/client/index.ts` 的 catalogActions）。数据契约见后端 `web/delegations.py`。
 *
 * 不变量 #22：roster 是**投影**（后端读父事件流派生），本组件不持有独立真相。
 */

import { useEffect, useRef, useState } from 'react';
import { Bot, Check, ChevronDown, ChevronRight, Square, X } from 'lucide-react';
import type { ConversationState } from '../types';
import type { DelegationEntry } from '../lib/api';
import type { TraceDensity } from '../lib/density';
import type { Disclosure } from '../lib/disclosure';
import type { ReasoningDisclosureApi } from './ReasoningBlock';
import { useTickingNow } from '../hooks/useTickingNow';
import { formatCoarseDuration } from '../lib/format';
import { DELEGATION_STATUS_CLASS } from '../lib/delegationStatus';
import { Conversation } from './Conversation';

/** 委派四态 → 中文标签（状态色 class 后缀走 lib/delegationStatus 单一事实源）。
 *  #1307 起 export：看板执行中卡片的折叠行复用同一份映射（不复制第二份）。 */
export const STATUS_LABEL: Record<DelegationEntry['status'], string> = {
  running: '运行中',
  completed: '完成',
  failed: '失败',
  stopped: '已停止',
};

/** dsh 口径 token 缩写：≥1M → 'x.xM'，≥1000 → 'x.xK'，否则原样。 */
export function formatTokenCount(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}

/** 诚实口径（issue §6.1 / AC6）：usage_state 非 ok 时标注「估算」/「无数据」，
 *  不把下界或无数据当精确值卖（PRODUCT「真实优先于好看」）。
 *  唯一实现——roster 行与分栏头共用（分栏不为此造一个假 DelegationEntry）。 */
export function tokenText(
  usedTokens: number,
  usageState: DelegationEntry['usage_state'],
): string {
  if (usageState === 'no_data') return '无数据';
  const base = `${formatTokenCount(usedTokens)} tok`;
  return usageState === 'usage_only' ? `${base} · 估算` : base;
}

/** 耗时：running 用「已运行 起点→now」，其余用「起点→终点」；缺失/倒挂不显数字。
 *  不足 1 秒也不显——真实链路 started/finished 同刻落盘（阻塞串行委派，
 *  `multiagent/tools.py` 明示），差值为 0 ⇒ 「耗时 0秒」是假精度。 */
export function durationText(entry: DelegationEntry, now: number): string | null {
  if (!entry.started_at) return null;
  const start = new Date(entry.started_at).getTime();
  const end = entry.completed_at ? new Date(entry.completed_at).getTime() : now;
  const ms = end - start;
  if (!Number.isFinite(ms) || ms < 1000) return null;
  const label = entry.status === 'running' ? '运行' : '耗时';
  const value = formatCoarseDuration(ms);
  return value === null ? null : `${label} ${value}`;
}

interface RosterProps {
  delegations: DelegationEntry[];
  /** 行主体点击 → 主视图打开（复用现有 handleSelect 语义）。 */
  onOpenSession: (childSessionId: string) => void;
  /** chevron → 右侧分栏打开（父会话保持不动）。 */
  onOpenSplit: (child: SplitChild) => void;
}

export function SubagentRoster({ delegations, onOpenSession, onOpenSplit }: RosterProps) {
  const [open, setOpen] = useState(false);
  // running 项耗时每秒 tick——放在**叶子组件**内，不外溢到 App 正文子树。
  const now = useTickingNow(1000);
  const rootRef = useRef<HTMLDivElement | null>(null);

  // 点外部关闭（浮层纪律，同 dsh dropdown）。
  useEffect(() => {
    if (!open) return;
    const onDocPointer = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDocPointer);
    return () => document.removeEventListener('mousedown', onDocPointer);
  }, [open]);

  // 无子智能体时不渲染入口（抄 dsh「无证据不渲染」；AC8）。
  if (delegations.length === 0) return null;

  const runningCount = delegations.filter((d) => d.status === 'running').length;

  return (
    <div
      className="roster-anchor"
      ref={rootRef}
      onKeyDown={(e) => {
        if (e.key === 'Escape' && open) {
          e.stopPropagation(); // 面板内 Esc 只收面板，不落到全局「中断流式」
          setOpen(false);
        }
      }}
    >
      <button
        className={`agent-trigger${open ? ' open' : ''}`}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-controls="subagent-roster"
        onClick={() => setOpen((v) => !v)}
        title="查看本会话的子任务"
      >
        <span className="roster-stack" aria-hidden="true">
          {delegations.slice(0, 3).map((d) => (
            <i
              key={d.child_session_id}
              className={`mini-avatar${d.status === 'running' ? ' running' : ''}`}
            >
              <Bot size={11} />
            </i>
          ))}
        </span>
        <span className="roster-label">{delegations.length} 个子任务</span>
        {runningCount > 0 && (
          <span className="roster-running-badge" title={`${runningCount} 个运行中`}>
            {runningCount}
          </span>
        )}
        <ChevronDown size={14} className="chev" />
      </button>

      {open && (
        <div className="roster" id="subagent-roster" role="dialog" aria-label="子任务列表">
          <div className="roster-head">
            子任务 <small>{delegations.length} 个</small>
            <button className="quiet" onClick={() => setOpen(false)}>
              收起
            </button>
          </div>
          {delegations.map((d) => {
            const duration = durationText(d, now);
            return (
              <div
                key={d.child_session_id}
                className="agent-row"
                role="button"
                tabIndex={0}
                onClick={() => {
                  // 选中即收面板（浮层纪律）：面板若留着会继续盖在分栏头部、
                  // 拦住「关闭分栏」等按钮的指针事件（e2e 实测）。
                  setOpen(false);
                  onOpenSession(d.child_session_id);
                }}
                onKeyDown={(e) => {
                  // 只在**行本身**聚焦时响应——chevron 按钮自己的 Enter/Space 不得冒泡成开主视图。
                  if (e.target !== e.currentTarget) return;
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    setOpen(false);
                    onOpenSession(d.child_session_id);
                  }
                }}
                title={`在主视图打开子会话 ${d.child_session_id}`}
              >
                <i className={`roster-status status-${d.status}`} aria-hidden="true" />
                <div className="agent-copy">
                  <div className="agent-title">{d.target || '?'}</div>
                  <div className="agent-sub">{d.task || '—'}</div>
                </div>
                <div className="agent-stats">
                  <span className="status-word">{STATUS_LABEL[d.status]}</span>
                  <span>{tokenText(d.used_tokens, d.usage_state)}</span>
                  {duration && <span>{duration}</span>}
                </div>
                <button
                  className="row-arrow"
                  aria-label={`在右侧分栏打开子会话 ${d.target || d.child_session_id}`}
                  title="右侧分栏打开（父会话保持不动）"
                  onClick={(e) => {
                    e.stopPropagation();
                    // 同上：分栏打开后收面板，否则面板盖住分栏头部（含关闭按钮）。
                    setOpen(false);
                    onOpenSplit({
                      childSessionId: d.child_session_id,
                      target: d.target,
                      status: d.status,
                      used_tokens: d.used_tokens,
                      usage_state: d.usage_state,
                    });
                  }}
                >
                  <ChevronRight size={15} />
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** 分栏打开的 child 荷载：路由 id + 票面 §5.2 分栏头三件（状态 pill / token 行）
 *  所需字段——`delegations` 行现成有（App.tsx 侧），不另查。 */
export interface SplitChild {
  childSessionId: string;
  target: string;
  status: DelegationEntry['status'];
  used_tokens: number;
  usage_state: DelegationEntry['usage_state'];
}

interface SplitPaneProps {
  child: SplitChild;
  conversation: ConversationState | null;
  error: string | null;
  density: TraceDensity;
  disclosure?: Disclosure;
  reasoningDisclosure?: ReasoningDisclosureApi;
  onClose: () => void;
}

/** 右侧分栏：子智能体的完整会话（复用 Conversation 同一渲染器，不建第二套）。
 *  分栏是**独立选择态**——不改变主会话当前路由，父会话（左列）原样保持。 */
export function SubagentSplitPane({
  child,
  conversation,
  error,
  density,
  disclosure,
  reasoningDisclosure,
  onClose,
}: SplitPaneProps) {
  // Esc 关闭此前挂在 <aside> 上，但 aside 不可聚焦 ⇒ 打开后键盘用户须先聚焦内部按钮
  // 才生效。挂载即聚焦托盘（tabIndex=-1 容器），Esc 立刻可用（Correctness P3）。
  const paneRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    paneRef.current?.focus();
  }, []);
  // 票面 §5.2 分栏头：[关闭×][状态 pill][子智能体标题][child id 短码]
  // [`12.4K tok · 运行中`]。token 走 tokenText 诚实口径（usage_only 标「估算」、
  // no_data 显「无数据」），与 roster 行同一实现。
  const usageLine = `${tokenText(child.used_tokens, child.usage_state)} · ${STATUS_LABEL[child.status]}`;
  return (
    <aside
      ref={paneRef}
      tabIndex={-1}
      className="subagent-split-pane"
      aria-label={`子会话 · ${child.target || child.childSessionId}`}
      onKeyDown={(e) => {
        if (e.key === 'Escape') {
          e.stopPropagation(); // 面板内 Esc 只关分栏，不落到全局「中断流式」
          onClose();
        }
      }}
    >
      <header className="subagent-split-head">
        <button className="close-pane" aria-label="关闭分栏" onClick={onClose} title="关闭分栏（Esc）">
          <X size={15} />
        </button>
        <span
          className={`act-status act-status-${DELEGATION_STATUS_CLASS[child.status]}`}
          aria-hidden="true"
          title={STATUS_LABEL[child.status]}
        >
          {child.status === 'completed' && <Check size={14} />}
          {child.status === 'failed' && <X size={14} />}
          {child.status === 'stopped' && <Square size={14} />}
          {child.status === 'running' && <span className="status-spinner" />}
        </span>
        <span className="split-title">{child.target || '子任务'}</span>
        <span className="split-subtitle" title={`child session ${child.childSessionId}`}>
          {child.childSessionId.slice(0, 8)}
        </span>
        <span className="split-usage">{usageLine}</span>
      </header>
      <div className="subagent-split-body">
        {error ? (
          <div className="subagent-split-status">子会话加载失败：{error}</div>
        ) : (
          <Conversation
            conversation={conversation}
            loadingHistory={conversation === null}
            density={density}
            disclosure={disclosure}
            reasoningDisclosure={reasoningDisclosure}
          />
        )}
      </div>
    </aside>
  );
}
