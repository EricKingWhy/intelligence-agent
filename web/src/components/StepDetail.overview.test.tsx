// @vitest-environment jsdom
/** #1419：Overview = 4 张真数据指标卡 + 8 个折叠段。
 *  只断言可观察行为：默认展开集、收起后内容仍在 DOM（inert）、指标值来自事件。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyEvent, initConversation } from '../lib/projection';
import { EventType } from '../types';
import type { ConversationState } from '../types';
import { ChatTab } from './StepDetail';

const EVENTS = [
  { type: EventType.RUN_STARTED, data: { model: 'gpt-x' }, seq: 1, run_id: 'r1', session_id: 'ov', time: '2026-10-20T10:00:00.000Z' },
  { type: EventType.MODEL_COMPLETED, data: { model: 'gpt-x', content: 'ok', step: 1 }, seq: 2, run_id: 'r1', step_id: 1, session_id: 'ov', time: '2026-10-20T10:00:01.000Z' },
  { type: EventType.MODEL_REQUEST, data: { outcome: 'completed', duration_ms: 1200, usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 }, cost_usd: '0.10' }, seq: 3, run_id: 'r1', step_id: 1, session_id: 'ov', time: '2026-10-20T10:00:02.000Z' },
  { type: EventType.MODEL_REQUEST, data: { outcome: 'completed', duration_ms: 800, usage: { prompt_tokens: 120, completion_tokens: 60, total_tokens: 180 }, cost_usd: '0.20' }, seq: 4, run_id: 'r1', step_id: 1, session_id: 'ov', time: '2026-10-20T10:00:03.000Z' },
  { type: EventType.RUN_COMPLETED, data: { usage_total: { prompt_tokens: 220, completion_tokens: 110, total_tokens: 330 }, cost_usd: 0.3 }, seq: 5, run_id: 'r1', session_id: 'ov', time: '2026-10-20T10:00:05.000Z' },
];

const conversation = (): ConversationState => EVENTS.reduce(applyEvent, initConversation('ov'));

let container: HTMLDivElement | null = null;
let root: Root | null = null;

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  container = null;
  root = null;
});

const render = () => act(() => root!.render(<ChatTab conversation={conversation()} tools={[]} />));
const section = (id: string) => container!.querySelector<HTMLElement>(`[data-ov-section="${id}"]`)!;
const click = (el: Element | null | undefined) => act(() => (el as HTMLElement).click());

describe('#1419 Overview', () => {
  it('4 张指标卡：值来自 session_usage_total / model/request 成本，不补 0', () => {
    render();
    const metrics = Array.from(container!.querySelectorAll<HTMLElement>('.ov-metric'));
    expect(metrics).toHaveLength(4);
    const text = metrics.map((m) => m.textContent ?? '');
    expect(text[0]).toContain('220');
    expect(text[1]).toContain('110');
    expect(text[2]).toContain('$0.3');
    expect(text[3]).toContain('5.0s');
  });

  it('P2-4：多 run 夹具 —— tokens/成本是会话累计，耗时是本轮，三者文案同作用域', () => {
    const multi = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'ov2', time: '2026-10-20T10:00:00.000Z' },
      { type: EventType.MODEL_REQUEST, data: { outcome: 'completed', duration_ms: 1000, usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 }, cost_usd: '0.10' }, seq: 2, run_id: 'r1', session_id: 'ov2', time: '2026-10-20T10:00:01.000Z' },
      { type: EventType.RUN_COMPLETED, data: { usage_total: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 }, cost_usd: 0.1 }, seq: 3, run_id: 'r1', session_id: 'ov2', time: '2026-10-20T10:00:02.000Z' },
      { type: EventType.RUN_STARTED, data: {}, seq: 4, run_id: 'r2', session_id: 'ov2', time: '2026-10-20T10:00:10.000Z' },
      { type: EventType.MODEL_REQUEST, data: { outcome: 'completed', duration_ms: 2000, usage: { prompt_tokens: 120, completion_tokens: 60, total_tokens: 180 }, cost_usd: '0.20' }, seq: 5, run_id: 'r2', session_id: 'ov2', time: '2026-10-20T10:00:11.000Z' },
      { type: EventType.RUN_COMPLETED, data: { usage_total: { prompt_tokens: 120, completion_tokens: 60, total_tokens: 180 }, cost_usd: 0.2 }, seq: 6, run_id: 'r2', session_id: 'ov2', time: '2026-10-20T10:00:12.000Z' },
    ];
    const conv = multi.reduce(applyEvent, initConversation('ov2'));
    act(() => root!.render(<ChatTab conversation={conv} tools={[]} />));
    const metrics = Array.from(container!.querySelectorAll<HTMLElement>('.ov-metric'));
    const text = metrics.map((m) => m.textContent ?? '');
    expect(text[0]).toContain('220'); // 会话累计（r1+r2），不是最近 run 的 120
    expect(text[1]).toContain('110');
    expect(text[2]).toContain('$0.30'); // 两笔 model/request 相加
    expect(text[0]).toContain('会话累计');
    expect(text[2]).toContain('会话成本');
    expect(text[3]).toContain('本轮耗时');
    expect(text[3]).toContain('2.0s');
    expect(text[3]).toContain('本轮最长单次 2.0s');
  });

  it('P3-12：缺失 usage 显示「—」，不补 0', () => {
    const missing = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'ov3', time: '2026-10-20T10:00:00.000Z' },
      { type: EventType.MODEL_REQUEST, data: { outcome: 'completed', duration_ms: 500 }, seq: 2, run_id: 'r1', session_id: 'ov3', time: '2026-10-20T10:00:01.000Z' },
      { type: EventType.RUN_COMPLETED, data: {}, seq: 3, run_id: 'r1', session_id: 'ov3', time: '2026-10-20T10:00:02.000Z' },
    ];
    const conv = missing.reduce(applyEvent, initConversation('ov3'));
    act(() => root!.render(<ChatTab conversation={conv} tools={[]} />));
    const values = Array.from(container!.querySelectorAll<HTMLElement>('.ov-metric-value')).map((el) => el.textContent);
    expect(values[0]).toBe('—');
    expect(values[1]).toBe('—');
    expect(values[2]).toBe('—');
    expect(values).not.toContain('0');
  });

  it('8 个折叠段；默认展开 RUN/TOOLS/决定/PERMISSION，其余收起且内容仍在 DOM', () => {
    render();
    expect(container!.querySelectorAll('.ov-acc')).toHaveLength(8);
    for (const id of ['run', 'tools', 'decide', 'permission']) {
      expect(section(id).classList.contains('open')).toBe(true);
    }
    for (const id of ['context', 'trace', 'model', 'checkpoint']) {
      expect(section(id).classList.contains('open')).toBe(false);
      const body = section(id).querySelector('.ov-acc-body')!;
      expect(body.getAttribute('aria-hidden')).toBe('true');
      expect(body.hasAttribute('inert')).toBe(true);
      expect(body.textContent?.length).toBeGreaterThan(0); // 收起不卸载
    }
    expect(section('checkpoint').textContent).toContain('后端未暴露');
  });

  /* #1419 O-3：待审批段内嵌的是**真实**审批卡（ApprovalCard 三钮 = postApproval 的
   * 三条既有决策），且整段带琥珀态；已失效（stale）与 App 记下的 404（goneApprovalIds）
   * 都不放按钮——那两类点下去只会换来一次注定失败的 POST。 */
  it('O-3：待审批 → 决定段琥珀态 + 真实审批卡；stale / gone 都不出卡', () => {
    const pending = (id: string, stale?: boolean) => ({
      approval_id: id,
      tool_name: 'bash',
      tool_call_id: 'tc-1',
      action_type: 'shell',
      title: '需要审批',
      description: 'rm -rf .cache/',
      arguments_preview: { command: 'rm -rf .cache/' },
      permission: 'ask',
      policy: 'standard',
      reason: '超出改动范围',
      allowed_decisions: ['approve', 'deny'],
      ...(stale === undefined ? {} : { stale }),
    });
    const withApprovals = (list: ReturnType<typeof pending>[]) => {
      const conv = conversation();
      return { ...conv, pending_approvals: list } as ConversationState;
    };

    // 待审批：琥珀态 + 真卡（卡内三个决策钮来自 ApprovalCard）
    act(() => root!.render(
      <ChatTab
        conversation={withApprovals([pending('ap-1')])}
        tools={[]}
        onApprovalGone={() => {}}
        onApprovalDecided={() => {}}
      />,
    ));
    expect(section('decide').classList.contains('is-attn')).toBe(true);
    expect(section('decide').textContent).toContain('1 项待审批');
    const card = section('decide').querySelector('.approval-card')!;
    expect(card).not.toBeNull();
    expect(card.textContent).toContain('rm -rf .cache/');

    // stale（run 已终结的孤儿）→ 不出卡，段也不是琥珀态
    act(() => root!.render(
      <ChatTab conversation={withApprovals([pending('ap-1', true)])} tools={[]} />,
    ));
    expect(section('decide').querySelector('.approval-card')).toBeNull();
    expect(section('decide').classList.contains('is-attn')).toBe(false);

    // App 已记下 404 → 同样不出卡
    act(() => root!.render(
      <ChatTab
        conversation={withApprovals([pending('ap-1')])}
        tools={[]}
        goneApprovalIds={new Set(['ap-1'])}
        onApprovalGone={() => {}}
        onApprovalDecided={() => {}}
      />,
    ));
    expect(section('decide').querySelector('.approval-card')).toBeNull();
  });

  it('点击标题开合；收起后 aria-hidden 与 inert 同步', () => {
    render();
    const run = section('run');
    expect(run.classList.contains('open')).toBe(true);
    click(run.querySelector('.ov-acc-head'));
    expect(run.classList.contains('open')).toBe(false);
    expect(run.querySelector('.ov-acc-body')!.getAttribute('aria-hidden')).toBe('true');
    expect(run.querySelector('.ov-acc-body')!.hasAttribute('inert')).toBe(true);

    const model = section('model');
    click(model.querySelector('.ov-acc-head'));
    expect(model.classList.contains('open')).toBe(true);
    expect(model.querySelector('.ov-acc-body')!.hasAttribute('inert')).toBe(false);
  });
});
