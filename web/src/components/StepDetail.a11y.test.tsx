// @vitest-environment jsdom
/** #1523：时间线键盘路径三件——tooltip 键盘可达（同一份派生）、Arrow 选中与 DOM
 *  焦点一致（含跨渲染窗口与切 tab）、行聚焦后 Enter 展开不受影响。
 *
 *  这些断言跨 `StepDetail`（moveSelection / 面板键位）与 Timeline 行渲染，必须用
 *  真实客户端渲染器；jsdom 不做键盘默认动作（Tab/点击不移焦），`activeElement`
 *  的每一次变化都只能来自被测代码显式 `.focus()`——这正是 AC2 要证的东西。
 */
import { useCallback, useState } from 'react';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { applyEvent, initConversation } from '../lib/projection';
import { EventType } from '../types';
import type { AgentEvent, ConversationState, ToolCall } from '../types';
import { StepDetail } from './StepDetail';
import type { InspectorFocus, InspectorPanelAction } from './StepDetail';

const KEYBOARD_EVENTS: AgentEvent[] = [
  { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'kb', time: '2026-10-20T10:00:00.000Z' },
  { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: { command: 'ls' } }, seq: 2, run_id: 'r1', step_id: 1, session_id: 'kb', time: '2026-10-20T10:00:01.000Z' },
  { type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: '{"ok":true}' }, seq: 3, run_id: 'r1', session_id: 'kb', time: '2026-10-20T10:00:02.000Z' },
  { type: EventType.TOOL_CALL, data: { tool_call_id: 't2', tool_name: 'bash', args: { command: 'pwd' } }, seq: 4, run_id: 'r1', step_id: 2, session_id: 'kb', time: '2026-10-20T10:00:03.000Z' },
  { type: EventType.TOOL_RESULT, data: { tool_call_id: 't2', content: '{"ok":true}' }, seq: 5, run_id: 'r1', session_id: 'kb', time: '2026-10-20T10:00:04.000Z' },
  { type: EventType.MODEL_REQUEST, data: { outcome: 'completed' }, seq: 6, run_id: 'r1', session_id: 'kb', time: '2026-10-20T10:00:05.000Z' },
  { type: EventType.MODEL_FAILED, data: { error: 'boom' }, seq: 7, run_id: 'r1', session_id: 'kb', time: '2026-10-20T10:00:06.000Z' },
  { type: EventType.RUN_COMPLETED, data: {}, seq: 8, run_id: 'r1', session_id: 'kb', time: '2026-10-20T10:00:07.000Z' },
];

const keyboardConversation = (): ConversationState =>
  KEYBOARD_EVENTS.reduce(applyEvent, initConversation('kb'));

/** 232 行：超出 `TIMELINE_WINDOW_DEFAULT`(200)，专测「选中项落到窗口外」的扩窗路径。 */
function bigConversation(): ConversationState {
  let conv = initConversation('big');
  const at = (i: number) => new Date(Date.parse('2026-10-20T10:00:00.000Z') + i * 10).toISOString();
  conv = applyEvent(conv, { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', session_id: 'big', time: at(0) });
  let seq = 2;
  for (let i = 0; i < 115; i++) {
    conv = applyEvent(conv, {
      type: EventType.TOOL_CALL,
      data: { tool_call_id: `t${i}`, tool_name: 'bash', args: { command: `echo ${i}` } },
      seq, run_id: 'r1', step_id: i + 1, session_id: 'big', time: at(seq),
    });
    seq++;
    conv = applyEvent(conv, {
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: `t${i}`, content: '{"ok":true}' },
      seq, run_id: 'r1', session_id: 'big', time: at(seq),
    });
    seq++;
  }
  conv = applyEvent(conv, { type: EventType.RUN_COMPLETED, data: {}, seq, run_id: 'r1', session_id: 'big', time: at(seq) });
  return conv;
}

function Host({ conv }: { conv: ConversationState }) {
  const [focus, setFocus] = useState<InspectorFocus>({ kind: 'run' });
  const [panel, setPanel] = useState({ pinned: false, expanded: false, width: 360, peekOpen: false });
  const [open, setOpen] = useState(true);
  const onFocusEvent = useCallback((event: AgentEvent) => {
    setFocus({ kind: 'event', event });
    setPanel((cur) => (cur.peekOpen ? cur : { ...cur, peekOpen: true }));
  }, []);
  const onFocusTool = useCallback((tool: ToolCall) => {
    setFocus({ kind: 'tool', tool });
  }, []);
  const onPanelAction = useCallback((a: InspectorPanelAction) => {
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
  }, []);
  return (
    <main className="app-regions" data-open={open ? 'true' : 'false'}>
      <StepDetail
        conversation={conv}
        streaming
        focus={focus}
        onFocusRun={() => setFocus({ kind: 'run' })}
        onFocusTool={onFocusTool}
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

beforeAll(() => {
  HTMLElement.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  container = null;
  root = null;
  document.body.innerHTML = '';
});

afterAll(() => {
  // beforeAll 的替换只在本文件生效；恢复原型，避免影响同进程其它测试文件。
  delete (HTMLElement.prototype as { scrollIntoView?: unknown }).scrollIntoView;
});

const render = (ui: React.ReactElement) => {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
};
const rows = () => Array.from(container!.querySelectorAll<HTMLElement>('button.timeline-row'));
const toolRows = () => Array.from(container!.querySelectorAll<HTMLElement>('button.detail-tool-row'));
const aside = () => container!.querySelector<HTMLElement>('aside.step-detail')!;
const key = (k: string) => act(() => {
  aside().dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
});
const rowKey = (k: string, el: HTMLElement) => act(() => {
  el.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
});
const click = (el: Element | null | undefined) => act(() => (el as HTMLElement).click());
const tabStops = (list: HTMLElement[]) => list.filter((r) => r.tabIndex === 0);
const tip = () => container!.querySelector<HTMLElement>('.tl-tooltip');

describe('#1523 AC2 — Arrow 选中与 DOM 焦点一致', () => {
  it('未交互时仅首行是 Tab 停靠点（roving tabindex，不是每行）', () => {
    render(<Host conv={keyboardConversation()} />);
    const list = rows();
    expect(list.length).toBeGreaterThan(1);
    // roving：无选中时停靠点 = 首行；其余行 tabIndex=-1。
    expect(tabStops(list)).toHaveLength(1);
    expect(tabStops(list)[0]).toBe(list[0]);
    expect(list[1].tabIndex).toBe(-1);
  });

  it('ArrowDown/ArrowUp 后 activeElement 与 aria-current 同一行，停靠点跟随', () => {
    render(<Host conv={keyboardConversation()} />);
    key('ArrowDown');
    expect(rows()[0].getAttribute('aria-current')).toBe('true');
    expect(document.activeElement).toBe(rows()[0]);
    key('ArrowDown');
    expect(rows()[1].getAttribute('aria-current')).toBe('true');
    expect(document.activeElement).toBe(rows()[1]);
    expect(document.activeElement?.getAttribute('aria-current')).toBe('true');
    // 停靠点跟随选中行：旧行不再是 Tab 停靠点。
    expect(tabStops(rows())).toHaveLength(1);
    expect(tabStops(rows())[0]).toBe(rows()[1]);
    key('ArrowUp');
    expect(document.activeElement).toBe(rows()[0]);
    expect(document.activeElement?.getAttribute('aria-current')).toBe('true');
  });

  it('跨渲染窗口：选中窗口外的行后，扩窗渲染并落焦到该行', () => {
    render(<Host conv={bigConversation()} />);
    const before = rows().length;
    expect(before).toBe(200); // TIMELINE_WINDOW_DEFAULT：窗口只渲染最近 200 行
    // 无选中时 ArrowDown 选中的是筛选结果的**最旧行**——初始在窗口外。
    key('ArrowDown');
    const after = rows().length;
    expect(after).toBeGreaterThan(before);
    const selected = rows().find((r) => r.getAttribute('aria-current') === 'true');
    expect(selected).toBeTruthy();
    expect(document.activeElement).toBe(selected);
  });

  it('切 tab 后 Arrow 落焦到可见实例的工具行', () => {
    render(<Host conv={keyboardConversation()} />);
    const chatTab = Array.from(container!.querySelectorAll<HTMLElement>('[role="tab"]'))
      .find((t) => t.textContent?.includes('概览'));
    expect(chatTab).toBeTruthy();
    click(chatTab);
    const list = toolRows();
    expect(list.length).toBeGreaterThan(1);
    key('ArrowDown');
    expect(list[0].getAttribute('aria-current')).toBe('true');
    expect(document.activeElement).toBe(list[0]);
    expect(tabStops(list)).toHaveLength(1);
    expect(tabStops(list)[0]).toBe(list[0]);
  });

  it('切到终端 tab 后 Arrow 同样落焦到可见实例的命令行（三个 ↑↓ 域同构）', () => {
    render(<Host conv={keyboardConversation()} />);
    const termTab = Array.from(container!.querySelectorAll<HTMLElement>('[role="tab"]'))
      .find((t) => t.textContent?.includes('终端'));
    expect(termTab).toBeTruthy();
    click(termTab);
    const list = Array.from(container!.querySelectorAll<HTMLElement>('button.step-head'));
    // KEYBOARD_EVENTS 里有两条 bash 调用（ls / pwd）。
    expect(list.length).toBe(2);
    key('ArrowDown');
    expect(list[0].getAttribute('aria-current')).toBe('true');
    expect(document.activeElement).toBe(list[0]);
    expect(tabStops(list)).toHaveLength(1);
    expect(tabStops(list)[0]).toBe(list[0]);
  });

  it('点击把选中挪走后，↑/↓ 回到上次键盘到过的行仍要落焦（同 key 不能短路）', () => {
    render(<Host conv={keyboardConversation()} />);
    // 键盘先到第二行：keyboardNav 记下 row[1]。
    key('ArrowDown');
    key('ArrowDown');
    expect(document.activeElement).toBe(rows()[1]);
    // 点击第一行：选中与 DOM 焦点都到 row[0]，keyboardNav 仍记着 row[1]。
    // （真实浏览器里点击会把焦点落在被点行上；jsdom 不模拟点击移焦，手动落焦。）
    click(rows()[0]);
    act(() => rows()[0].focus());
    expect(rows()[0].getAttribute('aria-current')).toBe('true');
    expect(document.activeElement).toBe(rows()[0]);
    // ↑/↓ 回到 row[1]：目标 key 与上次相同，state 必须仍是"新移动"，焦点要跟上。
    key('ArrowDown');
    expect(rows()[1].getAttribute('aria-current')).toBe('true');
    expect(document.activeElement).toBe(rows()[1]);
  });
});

describe('#1523 AC1 — tooltip 键盘可达且与指针同一份派生', () => {
  it('行聚焦后按 ? 打开 tooltip，行带 aria-describedby 指向它；再按 ? 关闭', () => {
    render(<Host conv={keyboardConversation()} />);
    act(() => rows()[0].focus());
    expect(document.activeElement).toBe(rows()[0]);
    rowKey('?', rows()[0]);
    const node = tip();
    expect(node).toBeTruthy();
    expect(node!.id).toBe('tl-tip');
    expect(node!.getAttribute('role')).toBe('tooltip');
    expect(rows()[0].getAttribute('aria-describedby')).toBe('tl-tip');
    // 必要信息在：标题 + meta（#seq · type 口径；首行是 run/started 事件）。
    expect(node!.querySelector('.tl-tooltip-title')?.textContent).toBeTruthy();
    expect(node!.querySelector('.tl-tooltip-meta')?.textContent).toContain('#1');
    expect(node!.querySelector('.tl-tooltip-meta')?.textContent).toContain('run/started');
    // 同键再按 = 关闭（显式开关键，不依赖焦点移动）。
    rowKey('?', rows()[0]);
    expect(tip()).toBeNull();
    expect(rows()[0].getAttribute('aria-describedby')).toBeNull();
  });

  it('Esc 关闭键盘 tooltip 且不冒泡成关面板；Enter 展开路径不受影响', () => {
    render(<Host conv={keyboardConversation()} />);
    key('ArrowDown');
    rowKey('?', rows()[0]);
    expect(tip()).toBeTruthy();
    // Esc 在行上按下：关掉 tooltip 这一层，不触发面板级 Esc（面板仍在）。
    rowKey('Escape', rows()[0]);
    expect(tip()).toBeNull();
    expect(container!.querySelector('aside.step-detail')).toBeTruthy();
    // 原 Enter 展开详情仍可用。
    key('Enter');
    expect(rows()[0].getAttribute('aria-expanded')).toBe('true');
  });

  it('键盘 tooltip 与指针 tooltip 同一行同内容（不为同一内容造第二份数据真相）', () => {
    render(<Host conv={keyboardConversation()} />);
    act(() => rows()[0].focus());
    rowKey('?', rows()[0]);
    const focusTitle = tip()!.querySelector('.tl-tooltip-title')!.textContent;
    const focusMeta = tip()!.querySelector('.tl-tooltip-meta')!.textContent;
    const focusLines = Array.from(tip()!.querySelectorAll('.tl-tooltip-line')).map((l) => l.textContent);
    // 焦点离开行清掉键盘 tip，再走指针 hover 路径。
    act(() => rows()[0].blur());
    expect(tip()).toBeNull();
    act(() => rows()[0].dispatchEvent(new MouseEvent('mouseover', { bubbles: true })));
    expect(tip()).toBeTruthy();
    expect(tip()!.querySelector('.tl-tooltip-title')!.textContent).toBe(focusTitle);
    expect(tip()!.querySelector('.tl-tooltip-meta')!.textContent).toBe(focusMeta);
    expect(Array.from(tip()!.querySelectorAll('.tl-tooltip-line')).map((l) => l.textContent))
      .toEqual(focusLines);
  });

  it('焦点离开行即收起键盘 tooltip；? 开在展开行上不显示（详情已摊开）', () => {
    render(<Host conv={keyboardConversation()} />);
    act(() => rows()[0].focus());
    // Enter 的既有契约是"开合选中卡"（#1419）：Tab 进入后先用 ↑/↓ 建立选中。
    key('ArrowDown');
    expect(rows()[0].getAttribute('aria-current')).toBe('true');
    rowKey('?', rows()[0]);
    expect(tip()).toBeTruthy();
    // Enter 展开同一行 ⇒ tip 是重复真相，随之隐藏。
    key('Enter');
    expect(rows()[0].getAttribute('aria-expanded')).toBe('true');
    expect(tip()).toBeNull();
    // 焦点移出列表 ⇒ 收起路径兜底（此态下 tip 本就已隐藏，断言 blur 分支不复活它）。
    act(() => rows()[0].blur());
    expect(tip()).toBeNull();
  });
});
