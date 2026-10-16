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
import { Bot, ChevronDown, ChevronRight, X } from 'lucide-react';
import type { ConversationState } from '../types';
import type { DelegationEntry } from '../lib/api';
import type { TraceDensity } from '../lib/density';
import type { Disclosure } from '../lib/disclosure';
import type { ReasoningDisclosureApi } from './ReasoningBlock';
import { useTickingNow } from '../hooks/useTickingNow';
import { formatCoarseDuration } from '../lib/format';
import { Conversation } from './Conversation';

/** 委派四态 → 中文标签（与 DelegationNode 的 STATUS_CLASS 同语义）。 */
const STATUS_LABEL: Record<DelegationEntry['status'], string> = {
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
 *  不把下界或无数据当精确值卖（PRODUCT「真实优先于好看」）。 */
export function tokenText(entry: DelegationEntry): string {
  if (entry.usage_state === 'no_data') return '无数据';
  const base = `${formatTokenCount(entry.used_tokens)} tok`;
  return entry.usage_state === 'usage_only' ? `${base} · 估算` : base;
}

/** 耗时：running 用「已运行 起点→now」，其余用「起点→终点」；缺失/倒挂不显数字。 */
export function durationText(entry: DelegationEntry, now: number): string | null {
  if (!entry.started_at) return null;
  const start = new Date(entry.started_at).getTime();
  const end = entry.completed_at ? new Date(entry.completed_at).getTime() : now;
  const label = entry.status === 'running' ? '运行' : '耗时';
  const value = formatCoarseDuration(end - start);
  return value === null ? null : `${label} ${value}`;
}

interface RosterProps {
  delegations: DelegationEntry[];
  /** 行主体点击 → 主视图打开（复用现有 handleSelect 语义）。 */
  onOpenSession: (childSessionId: string) => void;
  /** chevron → 右侧分栏打开（父会话保持不动）。 */
  onOpenSplit: (child: { childSessionId: string; target: string }) => void;
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
        title="查看本会话的子智能体"
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
        <span className="roster-label">{delegations.length} 个子智能体</span>
        {runningCount > 0 && (
          <span className="roster-running-badge" title={`${runningCount} 个运行中`}>
            {runningCount}
          </span>
        )}
        <ChevronDown size={14} className="chev" />
      </button>

      {open && (
        <div className="roster" id="subagent-roster" role="dialog" aria-label="子智能体列表">
          <div className="roster-head">
            子智能体 <small>{delegations.length} 个</small>
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
                onClick={() => onOpenSession(d.child_session_id)}
                onKeyDown={(e) => {
                  // 只在**行本身**聚焦时响应——chevron 按钮自己的 Enter/Space 不得冒泡成开主视图。
                  if (e.target !== e.currentTarget) return;
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
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
                  <span>{tokenText(d)}</span>
                  {duration && <span>{duration}</span>}
                </div>
                <button
                  className="row-arrow"
                  aria-label={`在右侧分栏打开子会话 ${d.target || d.child_session_id}`}
                  title="右侧分栏打开（父会话保持不动）"
                  onClick={(e) => {
                    e.stopPropagation();
                    onOpenSplit({ childSessionId: d.child_session_id, target: d.target });
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

interface SplitPaneProps {
  child: { childSessionId: string; target: string };
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
  return (
    <aside
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
        <span className="split-title">{child.target || '子智能体'}</span>
        <span className="split-subtitle mono" title={`child session ${child.childSessionId}`}>
          {child.childSessionId.slice(0, 8)}
        </span>
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
