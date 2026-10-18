// @vitest-environment jsdom
/** #1322：会话「重放」浮层（`SessionReplayDialog`）。
 *
 *  契约来源：issue #1322 AC-A1/A3 —— 入口有二次确认（讲清"零副作用地重放"）、
 *  回放过程可见（逐事件推进）、完成后可查看结果。本文件用文件级 jsdom 做真实客户端
 *  渲染（先例：ApprovalModal.test.tsx）：Radix Portal 内容进 document.body。
 *
 *  真实浏览器里的入口可见性 / 视觉由 e2e（`web/e2e/session-replay.spec.ts`）覆盖；
 *  这里锁的是**组件行为**：确认文案、请求触发、逐事件推进、失败留在原地。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, type ReactElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, replaySession: vi.fn() };
});

import { replaySession, SessionError } from '../lib/api';
import { SessionReplayDialog } from './SessionReplayDialog';

const STEPS = [
  { seq: 2, type: 'user/message', line: '[user] 列出文件' },
  { seq: 3, type: 'tool/call', line: '[tool] bash(command=ls)' },
  { seq: 4, type: 'tool/result', line: '  → result (frozen):\n  │ a.txt' },
  { seq: 5, type: 'model/completed', line: '[assistant] 有两个文件' },
];

let container: HTMLDivElement | null = null;
let root: Root | null = null;

function render(ui: ReactElement): void {
  if (!container) {
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  }
  act(() => root!.render(ui));
}

function buttons(): HTMLButtonElement[] {
  return [...document.body.querySelectorAll('button')] as HTMLButtonElement[];
}

function clickButton(label: string): void {
  const btn = buttons().find((b) => b.textContent?.trim() === label);
  if (!btn) throw new Error(`没有找到按钮：${label}（现有：${buttons().map((b) => b.textContent).join('|')}）`);
  act(() => {
    btn.dispatchEvent(new MouseEvent('click', { bubbles: true }));
  });
}

/** 轮询直到谓词成立（真实定时器；浮层的逐事件推进每步 60ms）。 */
async function waitFor(predicate: () => boolean, timeout = 3000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    if (predicate()) return;
    await act(async () => {
      await new Promise((r) => setTimeout(r, 15));
    });
  }
  throw new Error('waitFor 超时');
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  container?.remove();
  container = null;
  root = null;
  vi.mocked(replaySession).mockReset();
});

describe('SessionReplayDialog', () => {
  it('确认面讲清「零副作用地重放」，并显示会话标识', () => {
    render(
      <SessionReplayDialog target={{ id: 's1', title: '我的会话' }} onOpenChange={() => {}} />,
    );
    const text = document.body.textContent ?? '';
    expect(text).toContain('重放会话');
    expect(text).toContain('零副作用');
    expect(text).toContain('不写入');
    expect(text).toContain('我的会话');
    expect(text).toContain('s1'.slice(0, 12));
    // 确认态还没触发请求
    expect(replaySession).not.toHaveBeenCalled();
  });

  it('点「开始重放」→ 请求发出 → 逐事件推进 → 回放完成', async () => {
    vi.mocked(replaySession).mockResolvedValue({ session_id: 's1', count: 4, steps: STEPS });
    render(
      <SessionReplayDialog target={{ id: 's1', title: '我的会话' }} onOpenChange={() => {}} />,
    );

    clickButton('开始重放');
    expect(replaySession).toHaveBeenCalledWith('s1');

    // 逐事件推进：最终 4 步全出现，并给出完成态与计数
    await waitFor(() => (document.body.textContent ?? '').includes('回放完成'));
    const text = document.body.textContent ?? '';
    expect(text).toContain('[user] 列出文件');
    expect(text).toContain('→ result (frozen):');
    expect(text).toContain('[assistant] 有两个文件');
    expect(text).toContain('回放完成 · 4 个事件');
    expect(document.querySelectorAll('.replay-step')).toHaveLength(4);
  });

  it('推进是逐条可见的（不是一次性全渲染）', async () => {
    vi.mocked(replaySession).mockResolvedValue({ session_id: 's1', count: 4, steps: STEPS });
    render(
      <SessionReplayDialog target={{ id: 's1', title: '我的会话' }} onOpenChange={() => {}} />,
    );
    clickButton('开始重放');

    // 请求已 resolve、但推进刚开始：渲染步数应 < 总步数（逐事件推进的证据）。
    await waitFor(() => document.querySelectorAll('.replay-step').length >= 1);
    const during = document.querySelectorAll('.replay-step').length;
    expect(during).toBeLessThan(4);

    await waitFor(() => (document.body.textContent ?? '').includes('回放完成'));
    expect(document.querySelectorAll('.replay-step')).toHaveLength(4);
  });

  it('空会话：合法但无可回放内容', async () => {
    vi.mocked(replaySession).mockResolvedValue({ session_id: 's1', count: 0, steps: [] });
    render(
      <SessionReplayDialog target={{ id: 's1', title: '' }} onOpenChange={() => {}} />,
    );
    clickButton('开始重放');
    await waitFor(() => (document.body.textContent ?? '').includes('没有可回放的内容'));
    expect(document.body.textContent).toContain('回放完成 · 0 个事件');
  });

  it('失败：后端 detail 留在原地，可重试（回到确认态）', async () => {
    vi.mocked(replaySession).mockRejectedValue(new SessionError(404, "session 's1' not found"));
    render(
      <SessionReplayDialog target={{ id: 's1', title: '我的会话' }} onOpenChange={() => {}} />,
    );
    clickButton('开始重放');

    await waitFor(() => (document.body.textContent ?? '').includes("session 's1' not found"));
    // 回到可重试态
    expect(buttons().some((b) => b.textContent?.trim() === '重试')).toBe(true);
  });
});
