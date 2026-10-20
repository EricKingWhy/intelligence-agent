// @vitest-environment jsdom
/** #1429 中间对话区重设计的**渲染层**契约（纯 lib 规则在 lib/toolGroups.test.ts /
 *  lib/turnNav.test.ts 里已表驱动覆盖，这里只测 DOM 落地）：
 *
 *  - 工具分组：≥2 个同类工具折叠成一行组头，展开/收起受 Conversation 层 `key → open`
 *    控制（虚拟化滚出再滚回不丢状态——状态不在组内，组件被卸载也无所谓）；
 *  - 失败卡：关键错误行默认可见，且**没有**工具级重试按钮（P0-1 裁决）；
 *  - 参数中间省略：超长参数挂 `data-full`（完整值走 tooltip），头尾两段都渲染；
 *  - 轮次锚点：`#` 只复制 `#turn-N` 文本；
 *  - 轮次导航：视图起始晚于 1 时，之前的轮次药丸置灰划线，措辞区分"已压缩"。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { renderToStaticMarkup } from 'react-dom/server';
import { Conversation, TurnView } from './Conversation';
import { initConversation } from '../lib/projection';
import type { Turn, ToolCall } from '../types';

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
    const closed = renderToStaticMarkup(
      <TurnView
        turn={GROUPED_TURN}
        model={null}
        density="balanced"
        groupOpen={new Map([['c1', false]])}
        onToggleGroup={() => {}}
      />,
    );
    expect(closed).toContain('aria-expanded="false"');
    // 收起态：子树仍在 DOM（grid 0fr 动画），但容器不带 is-open
    expect(closed).not.toContain('disc-body is-open');

    const open = renderToStaticMarkup(
      <TurnView
        turn={GROUPED_TURN}
        model={null}
        density="balanced"
        groupOpen={new Map([['c1', true]])}
        onToggleGroup={() => {}}
      />,
    );
    expect(open).toContain('disc-body is-open');
  });

  it('不同 kind 相邻不成组（bash → read）', () => {
    const mixed = turn({
      tools: [tool({ tool_call_id: 'b1', name: 'bash' }), tool({ tool_call_id: 'r1', name: 'read_file' })],
      activities: [
        { kind: 'tool' as const, tool_call_id: 'b1' },
        { kind: 'tool' as const, tool_call_id: 'r1' },
      ],
    });
    const html = renderToStaticMarkup(<TurnView turn={mixed} model={null} density="balanced" />);
    expect(html).not.toContain('act-group');
  });
});

describe('#1429 失败卡 — 默认可见的错误行', () => {
  const FAILED = turn({
    tools: [
      tool({
        tool_call_id: 'f1',
        name: 'bash',
        status: 'failed',
        result: { error: 'permission denied', error_code: 'EACCES' },
      }),
    ],
    activities: [{ kind: 'tool' as const, tool_call_id: 'f1' }],
  });

  it('关键错误行与错误码默认在 L0 露出', () => {
    const html = renderToStaticMarkup(<TurnView turn={FAILED} model={null} density="balanced" />);
    expect(html).toContain('act-error-line');
    expect(html).toContain('act-error-code');
    expect(html).toContain('EACCES');
    expect(html).toContain('is-fail');
  });

  it('不给工具级重试按钮（P0-1 裁决：只呈现错误）', () => {
    const html = renderToStaticMarkup(<TurnView turn={FAILED} model={null} density="balanced" />);
    expect(html).not.toContain('重试');
  });
});

describe('#1429 参数中间省略', () => {
  it('超长参数：头尾两段 + data-full 承载完整值', () => {
    const long = 'x'.repeat(60) + '/tmp/output.txt';
    const t = turn({
      tools: [tool({ tool_call_id: 'a1', name: 'read_file', args: { path: long } })],
      activities: [{ kind: 'tool' as const, tool_call_id: 'a1' }],
    });
    const html = renderToStaticMarkup(<TurnView turn={t} model={null} density="balanced" />);
    expect(html).toContain('act-args-head');
    expect(html).toContain('act-args-tail');
    expect(html).toContain('data-full');
    expect(html).toContain('tabindex="0"');
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
    await act(async () => {
      btn.click();
    });
    expect(writeText).toHaveBeenCalledWith('#turn-3');
    expect(btn.textContent).toBe('已复制');
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
