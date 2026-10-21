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
import { applyEvent, initConversation, projectHistory } from '../lib/projection';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { StepDetail } from './StepDetail';
import type { InspectorFocus, InspectorPanelAction } from './StepDetail';

vi.mock('../lib/timelineView', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/timelineView')>();
  return {
    ...actual,
    deriveTimelineModel: vi.fn(actual.deriveTimelineModel),
    deriveTimelineView: vi.fn(actual.deriveTimelineView),
  };
});

import { deriveTimelineModel, deriveTimelineView } from '../lib/timelineView';

const deriveTimelineModelSpy = vi.mocked(deriveTimelineModel);
const deriveTimelineViewSpy = vi.mocked(deriveTimelineView);

const EVENTS: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:00.000Z' },
  { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: { command: 'ls' } }, seq: 2, run_id: 'r1', step_id: 1, session_id: 'tl', time: '2026-10-20T10:00:01.000Z' },
  { type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: '{"ok":true}' }, seq: 3, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:02.500Z' },
  { type: EventType.MODEL_REQUEST, data: { outcome: 'completed', duration_ms: 900 }, seq: 4, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:03.000Z' },
  { type: EventType.RUN_COMPLETED, data: {}, seq: 5, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:05.000Z' },
];

const KEYBOARD_EVENTS: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:00.000Z' },
  { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: { command: 'ls' } }, seq: 2, run_id: 'r1', step_id: 1, session_id: 'keyboard', time: '2026-10-20T10:00:01.000Z' },
  { type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: '{"ok":true}' }, seq: 3, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:02.000Z' },
  { type: EventType.TOOL_CALL, data: { tool_call_id: 't2', tool_name: 'bash', args: { command: 'pwd' } }, seq: 4, run_id: 'r1', step_id: 2, session_id: 'keyboard', time: '2026-10-20T10:00:03.000Z' },
  { type: EventType.TOOL_RESULT, data: { tool_call_id: 't2', content: '{"ok":true}' }, seq: 5, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:04.000Z' },
  { type: EventType.MODEL_REQUEST, data: { outcome: 'completed' }, seq: 6, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:05.000Z' },
  { type: EventType.MODEL_REQUEST, data: { outcome: 'completed' }, seq: 7, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:06.000Z' },
  { type: EventType.MODEL_FAILED, data: { error: 'first failure' }, seq: 8, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:07.000Z' },
  { type: EventType.MODEL_FAILED, data: { error: 'second failure' }, seq: 9, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:08.000Z' },
  { type: EventType.RUN_COMPLETED, data: {}, seq: 10, run_id: 'r1', session_id: 'keyboard', time: '2026-10-20T10:00:09.000Z' },
];

const conversation = (): ConversationState => EVENTS.reduce(applyEvent, initConversation('tl'));
const keyboardConversation = (): ConversationState => KEYBOARD_EVENTS.reduce(applyEvent, initConversation('keyboard'));

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
      <button className="host-toggle" onClick={() => setOpen((v) => !v)}>toggle</button>
      <StepDetail
        conversation={conv}
        streaming
        focus={focus}
        onFocusRun={() => setFocus({ kind: 'run' })}
        onFocusTool={() => undefined}
        onFocusEvent={onFocusEvent}
        panel={panel}
        open={open}
        onPanelAction={onPanelAction}
      />
    </main>
  );
}

let container: HTMLDivElement | null = null;
let root: Root | null = null;

/** P2-3 多槽夹具：A/B 两个会话共用同一个 Inspector（面板保持展开，模拟钉住）。 */
function SwitchHost({ a, b }: { a: ConversationState; b: ConversationState }) {
  const [active, setActive] = useState(a);
  const [focus, setFocus] = useState<InspectorFocus>({ kind: 'run' });
  const [panel, setPanel] = useState({ pinned: true, expanded: false, width: 360, peekOpen: false });
  const onFocusEvent = useCallback((event: AgentEvent) => {
    setFocus({ kind: 'event', event });
    setPanel((cur) => (cur.peekOpen ? cur : { ...cur, peekOpen: true }));
  }, []);
  const onPanelAction = useCallback((action: InspectorPanelAction) => {
    setPanel((cur) => {
      if (action.type === 'close-peek') return { ...cur, peekOpen: false };
      return cur;
    });
  }, []);
  const pick = (next: ConversationState) => {
    setFocus({ kind: 'run' });
    setActive(next);
  };
  return (
    <main className="app-regions">
      <button className="host-a" onClick={() => pick(a)}>A</button>
      <button className="host-b" onClick={() => pick(b)}>B</button>
      <StepDetail
        conversation={active}
        streaming
        focus={focus}
        onFocusRun={() => setFocus({ kind: 'run' })}
        onFocusTool={() => undefined}
        onFocusEvent={onFocusEvent}
        panel={panel}
        open
        onPanelAction={onPanelAction}
      />
    </main>
  );
}

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  deriveTimelineModelSpy.mockClear();
  deriveTimelineViewSpy.mockClear();
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
  it.each([
    ['all', '全部'],
    ['tool', '工具'],
    ['model', '模型'],
    ['fail', '失败'],
  ] as const)('#1451 production keyboard navigation follows the $0 filtered rows', (_filter, label) => {
    render(<Host conv={keyboardConversation()} />);
    expect(deriveTimelineModelSpy).toHaveBeenCalledTimes(1);
    expect(deriveTimelineViewSpy).toHaveBeenCalledTimes(1);

    const chip = Array.from(container!.querySelectorAll<HTMLElement>('.tl-chip'))
      .find((candidate) => candidate.textContent?.includes(label))!;
    click(chip);

    const filteredRows = rows();
    expect(filteredRows.length).toBeGreaterThan(1);
    expect(deriveTimelineModelSpy).toHaveBeenCalledTimes(1);
    expect(deriveTimelineViewSpy).toHaveBeenCalledTimes(_filter === 'all' ? 1 : 2);

    key('ArrowDown');
    expect(filteredRows[0].getAttribute('aria-current')).toBe('true');
    key('ArrowDown');
    expect(filteredRows[1].getAttribute('aria-current')).toBe('true');
    expect(filteredRows[0].getAttribute('aria-current')).toBeNull();
    key('ArrowUp');
    expect(filteredRows[0].getAttribute('aria-current')).toBe('true');
    key('ArrowDown');
    expect(filteredRows[1].getAttribute('aria-current')).toBe('true');

    const selectedKey = filteredRows[1].getAttribute('data-list-key');
    key('Enter');
    expect(filteredRows[1].getAttribute('aria-expanded')).toBe('true');
    expect(expandedCards()).toHaveLength(1);
    expect(expandedCards()[0].querySelector('button.timeline-row')?.getAttribute('data-list-key')).toBe(selectedKey);
  });

  it('#1451 a new append-only event version refreshes the base model', () => {
    const initial = conversation();
    render(<Host conv={initial} />);
    expect(deriveTimelineModelSpy).toHaveBeenCalledTimes(1);

    const appended = applyEvent(initial, {
      type: EventType.SESSION_RESUMED,
      data: {},
      seq: 6,
      session_id: 'tl',
      time: '2026-10-20T10:00:06.000Z',
    });
    render(<Host conv={appended} />);

    expect(deriveTimelineModelSpy).toHaveBeenCalledTimes(2);
    expect(rows()).toHaveLength(EVENTS.length + 1);
  });

  it('点击卡片：就地展开，`.timeline-row` 数量不变（窗口不因展开扩大）', () => {
    render(<Host conv={conversation()} />);
    const before = rows().length;
    expect(before).toBe(EVENTS.length);
    click(rows()[0]);
    expect(expandedCards()).toHaveLength(1);
    expect(rows()).toHaveLength(before);
    expect(rows()[0].getAttribute('aria-expanded')).toBe('true');
    // #1419 T-10：段头改为 `#seq · type`（原「钻取」标签退役），仍必须是展开区首行。
    expect(expandedCards()[0].querySelector('.tl-drill')?.textContent).toContain('#1 · run/started');
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

  it('面板关闭清空展开；重新打开不残留', () => {
    render(<Host conv={conversation()} />);
    click(rows()[1]);
    expect(expandedCards()).toHaveLength(1);

    click(container!.querySelector('button[aria-label="关闭运行详情"]'));
    expect(container!.querySelector('main')!.classList.contains('inspector-closed')).toBe(true);

    click(container!.querySelector('.host-toggle'));
    expect(container!.querySelector('main')!.classList.contains('inspector-closed')).toBe(false);
    expect(expandedCards()).toHaveLength(0);
  });

  it('实时新增事件不清掉已有展开（按 eventKey 而非索引）', () => {
    render(<Host conv={conversation()} />);
    click(rows()[1]);
    const card = expandedCards()[0];
    expect(card).toBeTruthy();

    const live = applyEvent(conversation(), {
      type: EventType.TEXT_DELTA, data: { delta: 'x' }, seq: 6, run_id: 'r1', session_id: 'tl', time: '2026-10-20T10:00:06.000Z',
    });
    render(<Host conv={live} />);
    expect(rows()).toHaveLength(EVENTS.length + 1);
    expect(expandedCards()).toHaveLength(1);
    expect(expandedCards()[0].textContent).toContain('tool/call');
  });

  it('展开不扩大窗口：600 事件仍只挂 200 行', () => {
    let big = initConversation('big');
    for (let i = 0; i < 600; i++) {
      big = applyEvent(big, {
        type: EventType.TEXT_DELTA,
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

  it('取消 ToolResult 的标题与失败筛选计数一致', () => {
    const conv = projectHistory('tl-cancel', [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'cancel-run', session_id: 'tl-cancel', time: '2026-10-20T10:00:00.000Z' },
      {
        type: EventType.TOOL_CALL,
        data: { tool_call_id: 'tc-cancel', tool_name: 'bash', args: { command: 'sleep 1' } },
        seq: 2,
        run_id: 'cancel-run',
        step_id: 1,
        session_id: 'tl-cancel',
        time: '2026-10-20T10:00:01.000Z',
      },
      {
        type: EventType.TOOL_RESULT,
        data: {
          tool_call_id: 'tc-cancel',
          content: '{"ok":false,"message":"cancelled","data":{"cancelled":true}}',
        },
        seq: 3,
        run_id: 'cancel-run',
        step_id: 1,
        session_id: 'tl-cancel',
        time: '2026-10-20T10:00:02.000Z',
      },
    ] as AgentEvent[]);
    render(<Host conv={conv} />);

    const resultRow = rows().find((row) => row.textContent?.includes('tool/result'))!;
    expect(resultRow.textContent).toContain('已中断');
    const fail = container!.querySelector<HTMLElement>('.tl-chip-fail')!;
    expect(fail.textContent).toContain('0');
    click(fail);
    expect(rows()).toHaveLength(0);
  });

  it('P1-1：hover 卡片出现 tooltip（完整时间 + step）', () => {
    render(<Host conv={conversation()} />);
    act(() => {
      rows()[1].dispatchEvent(new MouseEvent('mouseover', { bubbles: true, cancelable: true }));
    });
    const tip = container!.querySelector<HTMLElement>('.tl-tooltip');
    expect(tip).not.toBeNull();
    expect(tip!.textContent).toMatch(/\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/);
    expect(tip!.textContent).toContain('step 1');
  });

  it('P2-1：同 run 多条 seq-less model/started —— 点一张只展开一张，DOM id 不重复', () => {
    const seqLess: ConversationState = ([
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'sl', time: '2026-10-20T10:00:00.000Z' },
      { type: EventType.MODEL_STARTED, data: { model: 'gpt' }, seq: null, run_id: 'r1', session_id: 'sl', time: '2026-10-20T10:00:01.000Z' },
      { type: EventType.MODEL_STARTED, data: { model: 'gpt' }, seq: null, run_id: 'r1', session_id: 'sl', time: '2026-10-20T10:00:02.000Z' },
      { type: EventType.RUN_COMPLETED, data: {}, seq: 4, run_id: 'r1', session_id: 'sl', time: '2026-10-20T10:00:03.000Z' },
    ] as AgentEvent[]).reduce(applyEvent, initConversation('sl'));
    render(<Host conv={seqLess} />);
    click(rows()[1]);
    expect(expandedCards()).toHaveLength(1);
    expect(rows()[1].getAttribute('aria-expanded')).toBe('true');
    expect(rows()[2].getAttribute('aria-expanded')).toBe('false');
    const ids = Array.from(container!.querySelectorAll<HTMLElement>('[id^="tl-expand-"]')).map((el) => el.id);
    expect(ids).toHaveLength(1);
    expect(rows()[1].getAttribute('aria-controls')).toBe(ids[0]);
    expect(rows()[2].getAttribute('aria-controls')).toBeNull();
    // 第二张也能独立展开（键不冲突）。
    click(rows()[2]);
    expect(expandedCards()).toHaveLength(2);
    expect(new Set(Array.from(container!.querySelectorAll<HTMLElement>('[id^="tl-expand-"]')).map((el) => el.id)).size).toBe(2);
  });

  it('P2-2：Space 不再抢按钮/链接的原生激活；卡片本体仍保留 Space=peek', () => {
    render(<Host conv={conversation()} />);
    const chip = Array.from(container!.querySelectorAll<HTMLElement>('.tl-chip'))
      .find((c) => c.textContent?.includes('工具'))!;
    const chipSpace = new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true });
    act(() => chip.dispatchEvent(chipSpace));
    expect(chipSpace.defaultPrevented).toBe(false);

    const rowSpace = new KeyboardEvent('keydown', { key: ' ', code: 'Space', bubbles: true, cancelable: true });
    act(() => rows()[1].dispatchEvent(rowSpace));
    expect(rowSpace.defaultPrevented).toBe(true);
  });

  it('P2-3：App 侧关闭（绕过 onPanelAction）也清空展开；重开无残留', () => {
    render(<Host conv={conversation()} />);
    click(rows()[1]);
    expect(expandedCards()).toHaveLength(1);
    click(container!.querySelector('.host-toggle')); // 模拟 rail 菜单 / 会话切换的外部关闭
    expect(container!.querySelector('main')!.getAttribute('data-open')).toBe('false');
    click(container!.querySelector('.host-toggle'));
    expect(container!.querySelector('main')!.getAttribute('data-open')).toBe('true');
    expect(expandedCards()).toHaveLength(0);
  });

  it('P2-3：A/B 会话各自维护展开集，切回不覆盖', () => {
    const a = EVENTS.map((e) => ({ ...e, session_id: 'sa' })).reduce(applyEvent, initConversation('sa'));
    const b = EVENTS.map((e) => ({ ...e, session_id: 'sb' })).reduce(applyEvent, initConversation('sb'));
    render(<SwitchHost a={a} b={b} />);
    click(rows()[1]);
    expect(expandedCards()).toHaveLength(1);

    click(container!.querySelector('.host-b'));
    click(rows()[2]);
    expect(expandedCards()).toHaveLength(1);

    click(container!.querySelector('.host-a'));
    expect(rows()[1].getAttribute('aria-expanded')).toBe('true');
    expect(rows()[2].getAttribute('aria-expanded')).toBe('false');

    click(container!.querySelector('.host-b'));
    expect(rows()[2].getAttribute('aria-expanded')).toBe('true');
    expect(rows()[1].getAttribute('aria-expanded')).toBe('false');
  });

  it('P3-9：同时展开上限 3 张，超出淘汰最早一张', () => {
    render(<Host conv={conversation()} />);
    click(rows()[0]);
    click(rows()[1]);
    click(rows()[2]);
    expect(expandedCards()).toHaveLength(3);
    click(rows()[3]);
    expect(expandedCards()).toHaveLength(3);
    expect(rows()[0].getAttribute('aria-expanded')).toBe('false');
    expect(rows()[1].getAttribute('aria-expanded')).toBe('true');
    expect(rows()[3].getAttribute('aria-expanded')).toBe('true');
  });

  it('P3-10：选中卡被筛选隐藏时 Enter 不展开（清筛选后不凭空出现）', () => {
    render(<Host conv={conversation()} />);
    click(rows()[3]); // model/request：选中 + 展开
    click(rows()[3]); // 收起，选中仍在
    expect(expandedCards()).toHaveLength(0);
    const chip = Array.from(container!.querySelectorAll<HTMLElement>('.tl-chip'))
      .find((c) => c.textContent?.includes('工具'))!;
    click(chip);
    expect(rows().some((r) => r.textContent?.includes('model/request'))).toBe(false);
    key('Enter');
    const all = Array.from(container!.querySelectorAll<HTMLElement>('.tl-chip'))
      .find((c) => c.textContent?.includes('全部'))!;
    click(all);
    expect(expandedCards()).toHaveLength(0);
  });
});
