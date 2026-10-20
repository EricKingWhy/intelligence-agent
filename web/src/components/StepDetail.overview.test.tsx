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
  it('4 张指标卡：值来自 usage_total / cost_usd，不补 0', () => {
    render();
    const metrics = Array.from(container!.querySelectorAll<HTMLElement>('.ov-metric'));
    expect(metrics).toHaveLength(4);
    const text = metrics.map((m) => m.textContent ?? '');
    expect(text[0]).toContain('220');
    expect(text[1]).toContain('110');
    expect(text[2]).toContain('$0.3');
    expect(text[3]).toContain('5.0s');
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
