// @vitest-environment jsdom
/** #1419：Timeline 卡片流交互（点击就地展开 / Enter 开合 / Esc 分层 / 筛选保留展开）。
 *
 *  这些行为跨 `StepDetail`（面板级键盘与展开状态）与 `TimelineTab`（渲染/筛选），
 *  必须用真实客户端渲染器；SSR 只证明首帧，证明不了开合与焦点。
 */
import { useCallback, useState } from 'react';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { applyEvent, initConversation } from '../lib/projection';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { StepDetail } from './StepDetail';
import type { InspectorFocus, InspectorPanelAction } from './StepDetail';

const EVENTS: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:00.000Z' },
  { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: { command: 'ls' } }, seq: 2, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:01.000Z' },
  { type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: '{"ok":true}' }, seq: 3, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:02.500Z' },
  { type: EventType.MODEL_REQUEST, data: { outcome: 'completed', duration_ms: 900 }, seq: 4, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:03.000Z' },
  { type: EventType.RUN_COMPLETED, data: {}, seq: 5, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:05.000Z' },
];

const conversation = (): ConversationState => EVENTS.reduce(applyEvent, initConversation('tl'));

function Host({ conv, onAction }: { conv: ConversationState; onAction?: (a: InspectorPanelAction) => void }) {
  const [focus, setFocus] = useState<InspectorFocus>({ kind: 'run' });
  const [panel, setPanel] = useState({ pinned: false, expanded: false, width: 360, peekOpen: false });
  const [open, setOpen] = useState(true);
  const onFocusEvent = useCallback((event: AgentEvent) => {
    setFocus({ kind: 'event', event });
    setPanel((cur) => (cur.peekOpen ? cur : { ...cur, peekOpen: true }));
  }, []);
  const onPanelAction = useCallback((a: InspectorPanelAction) => {
    onAction?.(a);
    if (a.type === 'close') {
      setOpen(false);
      return;
    }
    setPanel((cur) => {
      if (a.type === 'pin') return { ...cur, pinned: a.value };
      if (a.type === 'expand') return { ...cur, expanded: a.value };
      if (a.type === 'resize') return { ...cur, width: a.width };
      if (a.type === 'open-peek') return { ...cur, peekOpen: true };
      if (a.type === 'close-peek') return { ...cur, peekOpen: false };
      return cur;
    });
  }, [onAction]);
  return (
    <main className={`app-regions${open ? '' : ' inspector-closed'}`} data-open={open ? 'true' : 'false'}>
      <StepDetail
        conversation={conv}
        streaming
        focus={focus}
        onFocusRun={() => setFocus({ kind: 'run' })}
        onFocusTool={() => undefined}
        onFocusEvent={onFocusEvent}
        panel={panel}
        onPanelAction={onPanelAction}
      />
    </main>
  );
}

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

const render = (ui: React.ReactElement) => act(() => root!.render(ui));
const rows = () => Array.from(container!.querySelectorAll<HTMLElement>('button.timeline-row'));
const expandedCards = () => Array.from(container!.querySelectorAll<HTMLElement>('.timeline-card.expanded'));
const aside = () => container!.querySelector<HTMLElement>('aside.step-detail')!;
const key = (k: string) => act(() => {
  aside().dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
});
const click = (el: Element | null | undefined) => act(() => (el as HTMLElement).click());

describe('#1419 Timeline 卡片流交互', () => {
  it('点击卡片：就地展开，`.timeline-row` 数量不变（窗口不因展开扩大）', () => {
    render(<Host conv={conversation()} />);
    const before = rows().length;
    expect(before).toBe(EVENTS.length);
    click(rows()[0]);
    expect(expandedCards()).toHaveLength(1);
    expect(rows()).toHaveLength(before);
    expect(rows()[0].getAttribute('aria-expanded')).toBe('true');
    expect(expandedCards()[0].textContent).toContain('钻取');
    // 点击同一张卡片 = 收起。
    click(rows()[0]);
    expect(expandedCards()).toHaveLength(0);
  });

  it('↑/↓ 只切换选中卡片，并保持 peek 关闭', () => {
    render(<Host conv={conversation()} />);
    click(rows()[0]);
    expect(rows()[0].getAttribute('aria-current')).toBe('true');
    key('ArrowDown');
    expect(rows()[1].getAttribute('aria-current')).toBe('true');
    expect(rows()[0].getAttribute('aria-current')).toBeNull();
    expect(container!.querySelector('.detail-peek')?.getAttribute('data-peek')).toBe('off');
  });

  it('Enter 开合选中卡片；Esc 先收展开，再关面板', () => {
    render(<Host conv={conversation()} />);
    click(rows()[1]); // 选中工具卡并展开
    expect(expandedCards()).toHaveLength(1);

    key('Enter');
    expect(expandedCards()).toHaveLength(0);
    key('Enter');
    expect(expandedCards()).toHaveLength(1);

    key('Escape');
    expect(expandedCards()).toHaveLength(0);
    expect(container!.querySelector('main')!.getAttribute('data-open')).toBe('true');

    key('Escape');
    expect(container!.querySelector('main')!.getAttribute('data-open')).toBe('false');
  });

  it('筛选计数与筛选结果同源；展开状态跨筛选保留', () => {
    render(<Host conv={conversation()} />);
    const chips = Array.from(container!.querySelectorAll<HTMLElement>('.tl-chip'));
    const chip = (label: string) => chips.find((c) => c.textContent?.includes(label))!;
    expect(chip('全部').textContent).toContain('5');
    expect(chip('工具').textContent).toContain('2');
    expect(chip('模型').textContent).toContain('1');

    click(rows()[1]); // 工具卡展开
    expect(expandedCards()).toHaveLength(1);

    click(chip('模型'));
    expect(rows()).toHaveLength(1);
    expect(rows()[0].textContent).toContain('model/request');

    click(chip('全部'));
    expect(rows()).toHaveLength(EVENTS.length);
    expect(expandedCards()).toHaveLength(1); // 筛选没有丢掉展开
  });

  it('展开不扩大窗口：600 事件仍只挂 200 行', () => {
    let big = initConversation('big');
    for (let i = 0; i < 600; i++) {
      big = applyEvent(big, {
        type: EventType.MODEL_DELTA,
        data: { delta: 'x' },
        seq: i,
        run_id: 'r1',
        step_id: 1,
        session_id: 'big',
        time: new Date(Date.UTC(2026, 9, 20, 10, 0, i % 60)).toISOString(),
      });
    }
    render(<Host conv={big} />);
    expect(rows()).toHaveLength(200);
    click(rows()[0]);
    expect(expandedCards()).toHaveLength(1);
    expect(rows()).toHaveLength(200);
  });

  it('失败筛选按状态判据：无失败时空态可一键清除', () => {
    render(<Host conv={conversation()} />);
    const fail = Array.from(container!.querySelectorAll<HTMLElement>('.tl-chip'))
      .find((c) => c.textContent?.includes('失败'))!;
    click(fail);
    expect(rows()).toHaveLength(0);
    expect(container!.querySelector('.tl-empty')).not.toBeNull();
    click(container!.querySelector('.tl-empty-reset'));
    expect(rows()).toHaveLength(EVENTS.length);
  });
});
