// @vitest-environment jsdom
/** #1419 M：命令面 = 段头汇总 + 效果图命令卡（卡头状态词 / 输出井着色 / live 卡 / 卡尾来源）。
 *
 *  只断言可观察行为与真实数据：计数来自 status，`exit · 耗时 · seq` 来自 result/时间戳/
 *  事件 seq，输出井的着色只按行内容（不改文本），live 卡只在 `status === 'running'` 出现。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';
import type { ToolCall } from '../types';
import { TerminalTab } from './StepDetail';

let container: HTMLDivElement | null = null;
let root: Root | null = null;

beforeAll(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
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

const noop = () => {};

/** 一次 bash 调用。`raw_call.seq` 是事件 seq（卡头/卡尾都回显它）。 */
const bash = (over: Partial<ToolCall> & { id: string; seq?: number }): ToolCall => {
  const { id, seq, ...rest } = over;
  return {
    tool_call_id: id,
    name: 'bash',
    args: { command: `echo ${id}` },
    status: 'success',
    result: { exit_code: 0, stdout: 'ok\n', stderr: '' },
    started_at: '2026-10-20T10:00:00.000Z',
    completed_at: '2026-10-20T10:00:02.400Z',
    raw_call: seq === undefined ? undefined : { seq },
    ...rest,
  } as ToolCall;
};

const render = (tools: ToolCall[]) =>
  act(() => root!.render(<TerminalTab tools={tools} onFocusTool={noop} />));
const cards = () => Array.from(container!.querySelectorAll<HTMLElement>('.term'));
const card = (i: number) => cards()[i];
/** 卡头按钮（`aria-expanded` 的唯一载体）。 */
const headOf = (i: number) => card(i)!.querySelector<HTMLElement>('.step-head')!;

describe('#1419 Terminal M-1 段头', () => {
  it('标签 + `N 条 · M 失败` + bash 胶囊，计数来自 status', () => {
    render([
      bash({ id: 'a', seq: 1 }),
      bash({ id: 'b', seq: 2, status: 'failed', result: { exit_code: 1, stdout: '', stderr: 'boom\n' } }),
      bash({ id: 'c', seq: 3, status: 'stopped' }),
    ]);
    const head = container!.querySelector('.pane-head')!;
    expect(head.textContent).toContain('命令历史');
    expect(head.textContent).toContain('3 条 · 1 失败');
    expect(head.textContent).toContain('bash');
    // stopped 不是失败（中断 ≠ 错误）——不许并进失败数
    expect(head.textContent).not.toContain('2 失败');
  });
});

describe('#1419 Terminal M-2 命令卡头', () => {
  it('`$ 命令` + 状态词 + `exit · 耗时 · seq`', () => {
    render([
      bash({ id: 'f', seq: 6, status: 'failed', result: { exit_code: 1, stdout: '', stderr: 'x\n' } }),
    ]);
    const c = card(0)!;
    expect(c.querySelector('.term-prompt')?.textContent).toBe('$');
    expect(c.querySelector('.step-title')?.textContent).toContain('echo f');
    expect(c.querySelector('.step-nums')?.textContent).toBe('fail');
    const meta = c.querySelector('.step-meta')!.textContent!;
    expect(meta).toContain('exit 1');
    expect(meta).toContain('2.4s');
    expect(meta).toContain('seq 6');
    // 图标方块按状态取色档：失败 = fail 红档
    expect(c.querySelector('.step-ico')?.className).toContain('fail');
  });

  it('成功卡 = ok 绿档 + 状态词 ok；中断卡不冒充 fail', () => {
    render([
      bash({ id: 's', seq: 1 }),
      bash({ id: 'st', seq: 2, status: 'stopped' }),
    ]);
    expect(card(0)!.querySelector('.step-nums')?.textContent).toBe('ok');
    expect(card(0)!.querySelector('.step-ico')?.className).toContain('ok');
    expect(card(1)!.querySelector('.step-nums')?.textContent).toBe('stop');
    expect(card(1)!.querySelector('.step-ico')?.className).toContain('warn');
    expect(card(1)!.querySelector('.step-meta')?.textContent).toContain('已中断');
  });

  it('默认展开：失败/中断/进行中展开，成功折叠（卡头已给出 exit/耗时/seq）', () => {
    render([
      bash({ id: 'ok', seq: 1 }),
      bash({ id: 'bad', seq: 2, status: 'failed' }),
      bash({ id: 'run', seq: 3, status: 'running', completed_at: undefined, result: undefined }),
    ]);
    /* `aria-expanded` 挂在**卡头按钮**上（`<section>` 不是控件，带这个属性无意义）。 */
    expect(headOf(0).getAttribute('aria-expanded')).toBe('false');
    expect(headOf(1).getAttribute('aria-expanded')).toBe('true');
    expect(headOf(2).getAttribute('aria-expanded')).toBe('true');
  });

  it('点卡头能开合（折叠体不卸载，与效果图同一套 grid-rows 动画）', () => {
    render([bash({ id: 'ok', seq: 1 })]);
    const head = headOf(0);
    act(() => head.click());
    expect(head.getAttribute('aria-expanded')).toBe('true');
    act(() => head.click());
    expect(head.getAttribute('aria-expanded')).toBe('false');
  });
});

describe('#1419 Terminal M-3 输出井', () => {
  it('按行内容着色：ERROR 红 / 点阵压暗 / 进度绿 / 普通行不着色，文本逐字不变', () => {
    /* 判定顺序照效果图 `termLines`：cmd → er → ok → dim。故 `[ 62%]` 那行是 ok
       （进度），**纯点阵行**（没有百分比）才是 dim；混着 `failed` 与 `passed` 的行
       命中 er（红优先于绿——效果图也是这么排的，不在移植时改语义）。 */
    const out = [
      'ERROR tests/api/test_sessions.py - fixture not found',
      '........',
      '........  [ 62%]',
      '1 failed, 41 passed in 8.42s',
      'plain line',
    ].join('\n');
    render([bash({ id: 'x', seq: 1, status: 'failed', result: { exit_code: 1, stdout: out, stderr: '' } })]);
    const spans = Array.from(card(0)!.querySelectorAll<HTMLElement>('.term-out span'));
    expect(spans.map((s) => s.className)).toEqual(['er', 'dim', 'ok', 'er', '']);
    // 着色不改文本
    expect(card(0)!.querySelector('.term-out')!.textContent).toBe(out);
  });

  it('stderr 内容也进井（不丢），卡尾如实说「stderr 有内容」', () => {
    render([
      bash({ id: 'e', seq: 4, status: 'failed', result: { exit_code: 1, stdout: 'out\n', stderr: 'bad\n' } }),
    ]);
    const well = card(0)!.querySelector('.term-out')!.textContent!;
    expect(well).toContain('out');
    expect(well).toContain('bad');
    expect(card(0)!.querySelector('.term-foot')!.textContent).toContain('stderr 有内容');
  });

  it('没有输出时说清楚（不画空井）', () => {
    render([bash({ id: 'n', seq: 1, result: { exit_code: 0, stdout: '', stderr: '' } })]);
    expect(card(0)!.querySelector('.term-out')).toBeNull();
    expect(card(0)!.textContent).toContain('这次调用没有输出');
  });
});

describe('#1419 Terminal M-4 live 卡', () => {
  it('进行中：状态词 live + 琥珀图标 + 光标 + 「跟随中 · 已运行」', () => {
    render([
      bash({
        id: 'run', seq: 22, status: 'running', completed_at: undefined, result: undefined,
        output: [{ channel: 'stdout', text: '........  [ 62%]' }],
      }),
    ]);
    const c = card(0)!;
    expect(c.querySelector('.step-nums')?.textContent).toBe('live');
    expect(c.querySelector('.step-ico')?.className).toContain('warn');
    expect(c.querySelector('.term-out')?.textContent).toContain('[ 62%]');
    expect(c.querySelector('.term-out .caret')).not.toBeNull();
    expect(c.querySelector('.term-live-dot')).not.toBeNull();
    expect(c.querySelector('.term-foot')?.textContent).toContain('跟随中 · 已运行');
    // 进行中没有 exit 码，不许编一个
    expect(c.querySelector('.step-meta')?.textContent).toContain('进行中');
    expect(c.querySelector('.step-meta')?.textContent).not.toContain('exit');
  });

  it('流式缓冲（tool.output）优先于 result——终态后 result 到达，流式内容不丢', () => {
    render([
      bash({
        id: 's', seq: 2, result: { exit_code: 0, stdout: 'final\n' },
        output: [{ channel: 'stdout', text: 'partial' }, { channel: 'stdout', text: '-more' }],
      }),
    ]);
    expect(card(0)!.querySelector('.term-out')!.textContent).toBe('partial-more');
  });
});

describe('#1419 Terminal M-5 卡尾', () => {
  it('「来源 seq N · stdout」+「查看事件」按钮真的回调', () => {
    const seen: ToolCall[] = [];
    act(() => root!.render(<TerminalTab tools={[bash({ id: 'j', seq: 6 })]} onFocusTool={(t) => seen.push(t)} />));
    const foot = card(0)!.querySelector('.term-foot')!;
    expect(foot.textContent).toContain('来源');
    expect(foot.textContent).toContain('seq 6');
    expect(foot.textContent).toContain('stdout');
    const jump = foot.querySelector<HTMLElement>('.ov-toggle');
    expect(jump).not.toBeNull();
    act(() => jump!.click());
    expect(seen).toHaveLength(1);
    expect(seen[0].tool_call_id).toBe('j');
  });

  it('非 bash 工具不进这个面', () => {
    render([{ tool_call_id: 'r', name: 'read', args: {}, status: 'success' } as ToolCall]);
    expect(cards()).toHaveLength(0);
    expect(container!.textContent).toContain('本次会话未执行命令');
  });
});
