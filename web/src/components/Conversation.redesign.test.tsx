// @vitest-environment jsdom
/** #1429 中间对话区重设计的**渲染层**契约（纯 lib 规则在 lib/toolGroups.test.ts /
 *  lib/turnNav.test.ts 里已表驱动覆盖，这里只测 DOM 落地）：
 *
 *  - 工具分组：≥2 个同类工具折叠成一行组头，展开/收起受 Conversation 层 `key → open`
 *    控制（虚拟化滚出再滚回不丢状态——状态不在组内，组件被卸载也无所谓）；
 *  - 失败卡：关键错误行默认可见（**经 projectHistory 投影**，用后端真实序列化信封），
 *    且没有工具级重试按钮（P0-1 裁决）；
 *  - 参数中间省略：超长参数挂 `data-full`（完整值走 tooltip），头尾两段都渲染；
 *  - 轮次锚点：`#` 只复制 `#turn-N` 文本；
 *  - 轮次导航：视图起始晚于 1 时，之前的轮次药丸置灰划线，措辞区分"已压缩"。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { Conversation, TurnView } from './Conversation';
import { initConversation, projectHistory } from '../lib/projection';
import type { AgentEvent, Turn, ToolCall } from '../types';

const T0 = '2026-09-18T00:00:00.000Z';

function tool(over: Partial<ToolCall> & Pick<ToolCall, 'tool_call_id' | 'name'>): ToolCall {
  return {
    args: {},
    status: 'success',
    result: 'ok',
    started_at: T0,
    completed_at: '2026-09-18T00:00:02.000Z',
    ...over,
  };
}

function turn(over: Partial<Turn> = {}): Turn {
  return {
    step_id: 1,
    user_message: 'hi',
    model: { text: 'answer', status: 'done' },
    segments: [{ text: 'answer', status: 'done' }],
    tools: [],
    activities: [{ kind: 'model', index: 0 }],
    status: 'done',
    turn_index: 1,
    user_message_seq: 1,
    started_at: T0,
    completed_at: '2026-09-18T00:00:03.000Z',
    ...over,
  };
}

/** 三个连续 bash 调用（同 kind ⇒ 成组）。 */
const BASH_TOOLS: ToolCall[] = [
  tool({ tool_call_id: 'c1', name: 'bash', args: { command: 'ls' } }),
  tool({ tool_call_id: 'c2', name: 'bash', args: { command: 'pwd' } }),
  tool({ tool_call_id: 'c3', name: 'bash', args: { command: 'whoami' } }),
];

const GROUPED_TURN: Turn = turn({
  tools: BASH_TOOLS,
  activities: BASH_TOOLS.map((t) => ({ kind: 'tool' as const, tool_call_id: t.tool_call_id })),
});

describe('#1429 工具分组 — 渲染落地', () => {
  it('三个连续 bash 折成一行组头 + 组内三张工具卡', () => {
    const html = renderToStaticMarkup(<TurnView turn={GROUPED_TURN} model={null} density="balanced" />);
    expect(html).toContain('act-group');
    expect(html).toContain('命令');
    // 组头是按钮：默认可展开（aria-expanded=true）
    expect(html).toContain('aria-expanded="true"');
    expect(html.match(/act-card/g)?.length).toBe(3);
  });

  it('开合状态由外部 Map 控制（虚拟化滚出再滚回不丢）', () => {
    const open = renderToStaticMarkup(
      <TurnView
        turn={GROUPED_TURN}
        model={null}
        density="balanced"
        groupOpen={new Map([['c1', true]])}
        onToggleGroup={() => {}}
      />,
    );
    expect(open).toContain('aria-expanded="true"');
    expect(open).toContain('disc-body is-open');
    expect(open).toMatch(/aria-controls="act-group-body-c1"/);

    const closed = renderToStaticMarkup(
      <TurnView
        turn={GROUPED_TURN}
        model={null}
        density="balanced"
        groupOpen={new Map([['c1', false]])}
        onToggleGroup={() => {}}
      />,
    );
    // 收起态：组头仍是按钮、仍声明展开态，但**没有**可展开的容器
    // （`Collapse` 未挂载过 ⇒ 子树不存在），也不该留一个悬空的 aria-controls。
    expect(closed).toContain('aria-expanded="false"');
    expect(closed).not.toContain('disc-body');
    expect(closed).not.toContain('aria-controls');
  });

  it('不同 kind 相邻不成组（bash → read）', () => {
    const mixed = turn({
      tools: [tool({ tool_call_id: 'b1', name: 'bash' }), tool({ tool_call_id: 'r1', name: 'read' })],
      activities: [
        { kind: 'tool' as const, tool_call_id: 'b1' },
        { kind: 'tool' as const, tool_call_id: 'r1' },
      ],
    });
    const html = renderToStaticMarkup(<TurnView turn={mixed} model={null} density="balanced" />);
    expect(html).not.toContain('act-group');
  });

  it('客户端收起：grid 过渡结束后子树真的从 DOM 卸载（不是只少一个 class）', async () => {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    const renderWith = (open: boolean) =>
      root.render(
        <TurnView
          turn={GROUPED_TURN}
          model={null}
          density="balanced"
          groupOpen={new Map([['c1', open]])}
          onToggleGroup={() => {}}
        />,
      );

    await act(async () => renderWith(true));
    const body = container.querySelector('.act-group-body') as HTMLElement;
    expect(body).toBeTruthy();

    await act(async () => renderWith(false));
    // 收起瞬间容器还在（过渡要跑），但已不带 is-open。
    expect(container.querySelector('.act-group-body')?.className ?? '').not.toContain('is-open');

    // 过渡收尾：`Collapse` 只认 grid-template-rows 的 transitionend。
    const ev = new Event('transitionend', { bubbles: true });
    Object.defineProperty(ev, 'propertyName', { value: 'grid-template-rows' });
    await act(async () => {
      (container.querySelector('.act-group-body') as HTMLElement).dispatchEvent(ev);
    });
    expect(container.querySelector('.act-group-body')).toBeNull();

    act(() => root.unmount());
    container.remove();
  });
});

describe('#1429 失败卡 — 默认可见的错误行（真实信封 + 真投影）', () => {
  /** 后端 `ToolResult.failure()` 的真实序列化形状（`model_dump_json()`）：
   *  `data` 恒为 null，error_code 只在信封里。前端旧夹具把它当对象直接挂在
   *  ToolCall 上，于是测的是一条生产不存在的路径（#1429 F1 假绿）。 */
  const FAILURE_ENVELOPE = JSON.stringify({
    ok: false,
    message: 'permission denied',
    data: null,
    error_code: 'TIMEOUT',
    retryable: false,
    metadata: {},
  });

  function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
    return { data: {}, seq: null, run_id: 'run-1', step_id: 1, time: T0, ...partial };
  }

  function projectedFailure(): Turn {
    const conv = projectHistory('s-fail', [
      ev({ type: 'user/message', seq: 1, data: { content: '看下权限' } }),
      ev({ type: 'run/started', seq: 2, data: { turn_index: 1 } }),
      ev({ type: 'tool/call', seq: 3, data: { tool_call_id: 'f1', tool_name: 'bash', args: { command: 'cat /root/x' } } }),
      ev({ type: 'tool/result', seq: 4, data: { tool_call_id: 'f1', content: FAILURE_ENVELOPE } }),
    ]);
    return conv.turns[0];
  }

  it('投影层把信封里的 error_code / message 落成 per-tool 事实', () => {
    const t = projectedFailure();
    expect(t.tools[0].status).toBe('failed');
    expect(t.tools[0].error).toEqual({ errorCode: 'TIMEOUT', message: 'permission denied' });
    // result 退化为 message 字符串（data 为 null）——正是 F1 指的丢字段路径。
    expect(t.tools[0].result).toBe('permission denied');
  });

  it('关键错误行与错误码默认在 L0 露出', () => {
    const html = renderToStaticMarkup(
      <TurnView turn={projectedFailure()} model={null} density="balanced" />,
    );
    expect(html).toContain('act-error-line');
    expect(html).toContain('act-error-code');
    expect(html).toContain('TIMEOUT');
    expect(html).toContain('permission denied');
    expect(html).toContain('is-fail');
  });

  it('成功路径不伪造错误行（ok=true 的信封没有 error_code 可显示）', () => {
    const conv = projectHistory('s-ok', [
      ev({ type: 'user/message', seq: 1, data: { content: '跑一下' } }),
      ev({ type: 'run/started', seq: 2, data: { turn_index: 1 } }),
      ev({ type: 'tool/call', seq: 3, data: { tool_call_id: 'g1', tool_name: 'bash', args: { command: 'ls' } } }),
      ev({
        type: 'tool/result',
        seq: 4,
        data: {
          tool_call_id: 'g1',
          content: JSON.stringify({ ok: true, message: '', data: { output: 'a\n' }, error_code: null }),
        },
      }),
    ]);
    expect(conv.turns[0].tools[0].error).toBeUndefined();
    const html = renderToStaticMarkup(
      <TurnView turn={conv.turns[0]} model={null} density="balanced" />,
    );
    expect(html).not.toContain('act-error-line');
  });

  it('不给工具级重试按钮：行内动作只有「复制输出」与「定位到运行详情」两个', async () => {
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(
        <TurnView
          turn={projectedFailure()}
          model={null}
          density="balanced"
          onFocusTool={() => {}}
        />,
      );
    });
    // 快捷动作在 hover / 聚焦后才挂载（#1429 F2）；键盘路径 = 聚焦行按钮。
    const row = container.querySelector('.act-node') as HTMLButtonElement;
    expect(row).toBeTruthy();
    await act(async () => row.focus());

    const acts = [...container.querySelectorAll('.act-quick-acts button')];
    expect(acts).toHaveLength(2);
    const labels = acts.map((b) => b.getAttribute('aria-label') ?? b.textContent ?? '');
    expect(labels.some((l) => l.includes('复制'))).toBe(true);
    expect(labels.some((l) => l.includes('定位到运行详情'))).toBe(true);
    expect(labels.some((l) => l.includes('重试'))).toBe(false);

    act(() => root.unmount());
    container.remove();
  });
});

describe('#1429 组头耗时（F7：中断的组不挂 ticker）', () => {
  const base = tool({ tool_call_id: 'x', name: 'bash', args: { command: 'ls' } });

  it('末成员被中断且没有 completed_at ⇒ 显式「已中断」，不续算一个停住的组', () => {
    const stopped = turn({
      tools: [
        { ...base, tool_call_id: 's1', status: 'stopped', completed_at: undefined },
        { ...base, tool_call_id: 's2', status: 'stopped', completed_at: undefined },
      ],
      activities: [
        { kind: 'tool' as const, tool_call_id: 's1' },
        { kind: 'tool' as const, tool_call_id: 's2' },
      ],
    });
    const html = renderToStaticMarkup(<TurnView turn={stopped} model={null} density="balanced" />);
    expect(html).toContain('已中断');
    // 没有终点就不该出现"已跑 3.2s"这类续算读数
    expect(html).not.toMatch(/act-group-total">[<\d]/);
  });

  it('running 组仍显示已运行时长（只有这一档挂 ticker）', () => {
    const running = turn({
      tools: [
        { ...base, tool_call_id: 'r1', status: 'success' },
        { ...base, tool_call_id: 'r2', status: 'running', completed_at: undefined },
      ],
      activities: [
        { kind: 'tool' as const, tool_call_id: 'r1' },
        { kind: 'tool' as const, tool_call_id: 'r2' },
      ],
    });
    const html = renderToStaticMarkup(<TurnView turn={running} model={null} density="balanced" />);
    expect(html).toContain('act-group-total');
    expect(html).not.toContain('已中断');
  });
});

describe('#1429 参数中间省略', () => {
  it('超长路径：头尾两段 + data-full 承载完整值', () => {
    const long = 'x'.repeat(60) + '/tmp/output.txt';
    const t = turn({
      tools: [tool({ tool_call_id: 'a1', name: 'read', args: { path: long } })],
      activities: [{ kind: 'tool' as const, tool_call_id: 'a1' }],
    });
    const html = renderToStaticMarkup(<TurnView turn={t} model={null} density="balanced" />);
    expect(html).toContain('act-args-head');
    expect(html).toContain('act-args-tail');
    expect(html).toContain(`data-full="${long}"`);
    // 参数行本身不可聚焦（span 嵌在行 button 里属于非法嵌套），键盘走行按钮。
    expect(html).not.toContain('tabindex="0"');
  });

  it('短参数不切两段、不挂 tooltip', () => {
    const t = turn({
      tools: [tool({ tool_call_id: 'a2', name: 'bash', args: { command: 'ls' } })],
      activities: [{ kind: 'tool' as const, tool_call_id: 'a2' }],
    });
    const html = renderToStaticMarkup(<TurnView turn={t} model={null} density="balanced" />);
    expect(html).not.toContain('act-args-tail');
    expect(html).not.toContain('data-full');
  });

  it('bash 长命令不预截断：data-full 是全量，tooltip 也显示全量（#1429 F5）', async () => {
    const command =
      'npm run build --workspace web --output-path /very/long/directory/name/for/testing/purposes';
    expect(command.length).toBeGreaterThan(64);
    const t = turn({
      tools: [tool({ tool_call_id: 'a3', name: 'bash', args: { command } })],
      activities: [{ kind: 'tool' as const, tool_call_id: 'a3' }],
    });
    const html = renderToStaticMarkup(<TurnView turn={t} model={null} density="balanced" />);
    expect(html).toContain('act-args-tail');
    expect(html).toContain(`data-full="${command}"`);

    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(<TurnView turn={t} model={null} density="balanced" />);
    });
    await act(async () => (container.querySelector('.act-node') as HTMLButtonElement).focus());
    const tip = document.body.querySelector('.arg-tip');
    expect(tip?.textContent).toBe(command);
    // #1429 复审 P3：tooltip 的触发者（行 button）必须自己带 `aria-describedby`；
    // 挂到不可聚焦的 `.act-args` span 上，读屏用户永远拿不到这层描述。
    const row = container.querySelector('.act-node') as HTMLButtonElement;
    expect(row.getAttribute('aria-describedby')).toBe(tip?.id);
    expect(container.querySelector('.act-args')?.hasAttribute('aria-describedby')).toBe(false);

    act(() => root.unmount());
    container.remove();
  });

  it('指针离开时焦点仍在卡内 ⇒ 快捷动作不卸载（Tab 到复制按钮不被抽走）', async () => {
    const t = turn({
      tools: [tool({ tool_call_id: 'a5', name: 'bash', args: { command: 'ls' }, result: 'out' })],
      activities: [{ kind: 'tool' as const, tool_call_id: 'a5' }],
    });
    const container = document.createElement('div');
    document.body.appendChild(container);
    const root = createRoot(container);
    await act(async () => {
      root.render(<TurnView turn={t} model={null} density="balanced" />);
    });
    const wrap = container.querySelector('.act-card') as HTMLElement;
    const row = container.querySelector('.act-node') as HTMLButtonElement;
    await act(async () => row.focus());
    expect(container.querySelector('.act-quick-acts')).not.toBeNull();

    // 焦点进卡内（快捷动作按钮），随后指针离开卡片区域。
    const actBtn = container.querySelector('.act-quick-acts button') as HTMLButtonElement;
    expect(actBtn).not.toBeNull();
    await act(async () => actBtn.focus());
    await act(async () => {
      wrap.dispatchEvent(new MouseEvent('mouseout', { bubbles: true }));
      wrap.dispatchEvent(new MouseEvent('mouseleave', { bubbles: false }));
    });
    expect(container.querySelector('.act-quick-acts')).not.toBeNull();

    act(() => root.unmount());
    container.remove();
  });
});

describe('#1429 轮次锚点', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('点 `#` 复制 `#turn-N` 文本（不承诺深链）', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    await act(async () => {
      root.render(<TurnView turn={turn({ turn_index: 3 })} turnIndex={3} model={null} density="balanced" />);
    });
    const btn = container.querySelector('.tm-anchor') as HTMLButtonElement;
    expect(btn).toBeTruthy();
    // #1429 F3：文案是「锚点」而不是「链接」——不承诺可分享的深链。
    expect(btn.title).toBe('复制轮次锚点');
    await act(async () => {
      btn.click();
    });
    expect(writeText).toHaveBeenCalledWith('#turn-3');
    expect(btn.textContent).toBe('已复制');
    expect(btn.title).toBe('已复制轮次锚点');
  });

  it('复制失败不显示「已复制」（#1429 P3：不假装成功）', async () => {
    const writeText = vi.fn().mockRejectedValue(new Error('denied'));
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } });

    await act(async () => {
      root.render(<TurnView turn={turn({ turn_index: 3 })} turnIndex={3} model={null} density="balanced" />);
    });
    const btn = container.querySelector('.tm-anchor') as HTMLButtonElement;
    await act(async () => {
      btn.click();
    });
    expect(btn.textContent).not.toBe('已复制');
    expect(btn.title).toBe('复制轮次锚点');
  });
});

describe('#1429 轮次导航', () => {
  let container: HTMLDivElement;
  let root: Root;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => root.unmount());
    container.remove();
  });

  it('视图从第 3 轮开始：1/2 置灰划线，措辞区分「已压缩」与「不在视图」', async () => {
    const conv = {
      ...initConversation('s1'),
      turns: [
        turn({ step_id: 3, turn_index: 3 }),
        turn({ step_id: 4, turn_index: 4 }),
      ],
      // 只压缩到第 2 轮：第 1 轮有压缩记录，第 2 轮没有（视图起始晚于 1）
      compactions: [{ compacted_turn_count: 1 } as never],
    };
    await act(async () => {
      root.render(<Conversation conversation={conv} loadingHistory={false} density="balanced" />);
    });
    const pills = [...container.querySelectorAll('.turn-nav-pill')];
    expect(pills).toHaveLength(4);
    expect(pills[0].className).toContain('is-off');
    expect(pills[0].getAttribute('title')).toContain('已压缩为摘要');
    expect(pills[1].className).toContain('is-off');
    expect(pills[1].getAttribute('title')).toContain('不在当前视图中');
    expect(pills[2].className).not.toContain('is-off');
    expect(pills[3].className).not.toContain('is-off');
  });

  it('单轮会话不渲染导航（没有可跳目标）', async () => {
    const conv = { ...initConversation('s2'), turns: [turn()] };
    await act(async () => {
      root.render(<Conversation conversation={conv} loadingHistory={false} density="balanced" />);
    });
    expect(container.querySelector('.turn-nav')).toBeNull();
  });
});
