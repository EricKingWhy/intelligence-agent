// @vitest-environment jsdom
/** #1308 卡片正文：最后一条 user / agent 消息。
 *
 *  票面 AC-19 的渲染面：形状抄 Orca `AgentKanbanCard.tsx:255-268`（`You` 前缀 +
 *  `line-clamp-1` / agent 名 + `line-clamp-2`）。本仓无 Tailwind，clamp 由
 *  `styles/kanban.css` 的 `.kanban-card-msg-*` 类承担——这里断言**类存在**，
 *  而不是断言浏览器算出的 `-webkit-line-clamp`（jsdom 不做布局）。
 *
 *  钉四件事：
 *  ① 两条都有 → 两行都渲染，前缀与文本正确；
 *  ② 只有 user / 只有 agent → 只渲染存在的那一行（不渲染空行）；
 *  ③ 都为空 → 正文区不存在（空会话仍显示原有提示）；
 *  ④ 截断口径：组件**不**在渲染侧再截断（后端写入侧已截断 128 字符）。
 */

import { act, type ReactNode } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { KanbanCard } from './KanbanCard';
import type { SessionSummary } from '../types';

const NOOP = () => {};

function summary(over: Partial<SessionSummary> = {}): SessionSummary {
  return {
    session_id: 's1',
    event_count: 3,
    first_event_time: '2026-10-17T00:00:00Z',
    last_event_time: '2026-10-17T00:00:00Z',
    first_user_message: '任务 s1',
    trace_id: null,
    trace_url: null,
    workspace: null,
    archived: false,
    corrupted: false,
    product_state: '',
    product_state_changed_at: null,
    attention_kind: null,
    attention_summary: null,
    last_user_message: null,
    last_agent_message: null,
    ...over,
  };
}

let host: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
});

afterEach(() => {
  act(() => root.unmount());
  host.remove();
});

function render(node: ReactNode) {
  act(() => {
    root.render(node);
  });
}

function renderCard(over: Partial<SessionSummary> = {}) {
  render(
    <KanbanCard
      session={summary(over)}
      now={Date.parse('2026-10-17T00:01:00Z')}
      draggable
      onDragStart={NOOP}
      unread={false}
      onOpen={NOOP}
    />,
  );
}

describe('#1308 卡片正文最后一条消息', () => {
  it('两条都有 → user 行（You 前缀 + clamp-1）与 agent 行（agent 前缀 + clamp-2）', () => {
    renderCard({ last_user_message: '改一下标题', last_agent_message: '已改好' });

    const user = host.querySelector('.kanban-card-msg-user');
    expect(user).not.toBeNull();
    expect(user?.textContent).toBe('You 改一下标题');
    expect(user?.className).toContain('kanban-card-msg-clamp-1');

    const agent = host.querySelector('.kanban-card-msg-agent');
    expect(agent).not.toBeNull();
    expect(agent?.textContent).toBe('agent 已改好');
    expect(agent?.className).toContain('kanban-card-msg-clamp-2');
  });

  it('只有 user → 只渲染 user 行（agent 行不出现，不留空行）', () => {
    renderCard({ last_user_message: '改一下标题' });
    expect(host.querySelector('.kanban-card-msg-user')?.textContent).toBe('You 改一下标题');
    expect(host.querySelector('.kanban-card-msg-agent')).toBeNull();
  });

  it('只有 agent → 只渲染 agent 行', () => {
    renderCard({ last_agent_message: '已改好' });
    expect(host.querySelector('.kanban-card-msg-user')).toBeNull();
    expect(host.querySelector('.kanban-card-msg-agent')?.textContent).toBe('agent 已改好');
  });

  it('都为空 → 正文区不渲染；空会话提示仍在', () => {
    renderCard({ first_user_message: null });
    expect(host.querySelector('.kanban-card-msg-user')).toBeNull();
    expect(host.querySelector('.kanban-card-msg-agent')).toBeNull();
    expect(host.querySelector('.kanban-card-hint')?.textContent).toContain('空会话');
  });

  it('渲染侧不二次截断：128 字符原样输出（截断是写入侧的职责）', () => {
    const exact = 'x'.repeat(128);
    renderCard({ last_user_message: exact, last_agent_message: exact });
    expect(host.querySelector('.kanban-card-msg-user')?.textContent).toBe(`You ${exact}`);
    expect(host.querySelector('.kanban-card-msg-agent')?.textContent).toBe(`agent ${exact}`);
  });

  it('页脚仍只有时间节点（AC-16 不回退）', () => {
    renderCard({ last_user_message: '改一下标题', last_agent_message: '已改好' });
    const foot = host.querySelector('.kanban-card-foot');
    expect(foot?.children).toHaveLength(1);
    expect(foot?.querySelector('time')).not.toBeNull();
  });
});
