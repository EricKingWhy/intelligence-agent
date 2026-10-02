/** FE-01（#148）：停顿等待提示的渲染契约。
 *
 *  SSR：本车道没有 effect（定时器不跑），所以「何时算停顿」的真相在
 *  `runState.waitingHintText` 的单测里，这里只钉渲染决策——阈值以下必须
 *  一个节点都不多（与现状逐字节一致），阈值以上必须带秒数与 CSS 挂点。
 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { WaitingHint } from './TopBar';
import { WAIT_HINT_IDLE_SEC } from '../lib/runState';

const html = (idleSec: number) => renderToStaticMarkup(createElement(WaitingHint, { idleSec }));

describe('WaitingHint', () => {
  it('阈值以下不渲染任何节点（阈值以上的提示不得在正常生成时冒出来）', () => {
    expect(html(0)).toBe('');
    expect(html(WAIT_HINT_IDLE_SEC - 1)).toBe('');
  });

  it('达到阈值渲染说明：含已等待秒数 + CSS 挂点', () => {
    const out = html(WAIT_HINT_IDLE_SEC);
    expect(out).toContain('wait-hint');
    expect(out).toContain(String(WAIT_HINT_IDLE_SEC));
  });

  it('不渲染进度/回退类断言（未被事件证实的话一句都不说）', () => {
    const out = html(95);
    for (const banned of ['%', '进度', '预计', 'ETA', '即将', '回退']) {
      expect(out).not.toContain(banned);
    }
  });
});

// ── #537：TopBar 预算徽标（已用/上限）——数据源是投影的 run 预算镜像
//    （run/started 回显 ceilings + 前端折叠的消耗账），徽标文本经
//    runBudget.budgetBadgeFacts 与暂停面板同源。这里用**真投影**折叠事件构造
//    conversation（不手搓 state），锁渲染决策：
//    设了 ceiling 的维才出徽标、≥80% 加 --warning、没配预算零变化。

import { TopBar } from './TopBar';
import { applyEvent, initConversation } from '../lib/projection';
import { EventType } from '../types';
import type { ConversationState } from '../types';
import type { TraceDensity } from '../lib/density';
import type { Theme } from '../lib/theme';

const baseProps = {
  streaming: false,
  inspectorOpen: false,
  onToggleInspector: () => {},
  density: 'balanced' as TraceDensity,
  onDensityChange: () => {},
  theme: 'dark' as Theme,
  onToggleTheme: () => {},
  authRequired: false,
  onOpenMemories: () => {},
  sessionId: null,
  onOpenContextUsage: () => {},
};

const BUDGET_ECHO = {
  run: {
    max_agent_turns_total: null,
    max_model_requests: null,
    max_total_tokens: 500000,
    max_cost_usd: null,
    deadline_at: null,
    tool_call_limits: {},
  },
};

function conversationWithBudget(echo: typeof BUDGET_ECHO | null, tokens: number): ConversationState {
  let s = initConversation('sess-0000');
  s = applyEvent(s, {
    type: EventType.RUN_STARTED,
    data: echo ? { budget: echo } : {},
    seq: 1, run_id: 'r1', step_id: null, time: '2026-10-02T10:00:00Z',
  });
  s = applyEvent(s, {
    type: EventType.MODEL_REQUEST,
    data: tokens > 0 ? { usage: { total_tokens: tokens } } : {},
    seq: 2, run_id: 'r1', step_id: 1, time: '2026-10-02T10:00:01Z',
  });
  return s;
}

const bar = (conversation: ConversationState | null) =>
  renderToStaticMarkup(createElement(TopBar, { ...baseProps, conversation }));

describe('TopBar 预算徽标（#537）', () => {
  it('设了 tokens ceiling → 「28000 / 500000 tok」徽标在场（同源文本，未配维不出现）', () => {
    const out = bar(conversationWithBudget(BUDGET_ECHO, 28000));
    expect(out).toContain('budget-badge');
    expect(out).toContain('28000 / 500000 tok');
    expect(out).not.toContain('turns');
    expect(out).not.toContain('req');
    expect(out).not.toContain('$');
  });

  it('≥80% → 徽标带 --warning 修饰类（静止预警）；低占比没有', () => {
    const warned = bar(conversationWithBudget(BUDGET_ECHO, 450000));
    expect(warned).toContain('budget-badge--warning');
    const calm = bar(conversationWithBudget(BUDGET_ECHO, 28000));
    expect(calm).not.toContain('budget-badge--warning');
  });

  it('没配 run 预算（run/started 不带 budget）→ 徽标整段不渲染（既有形态零变化）', () => {
    const out = bar(conversationWithBudget(null, 28000));
    expect(out).not.toContain('budget-badge');
  });

  it('100% 到顶 → 徽标照常渲染（PausedPanel 接管是另一条链路，TopBar 不崩不吞）', () => {
    const out = bar(conversationWithBudget(BUDGET_ECHO, 500000));
    expect(out).toContain('500000 / 500000 tok');
    expect(out).toContain('budget-badge--warning');
  });
});
