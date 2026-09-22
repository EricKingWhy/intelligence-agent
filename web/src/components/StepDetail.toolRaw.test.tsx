// @vitest-environment jsdom
/** 真机审计 B-D7 的回归锁：工具 Raw 档的两个「复制 Raw」按钮必须能按名字区分。
 *
 *  为什么要 jsdom：Raw 面板只在**选中 Raw 档**时渲染，`StepDetail.test.tsx` 是 SSR 车道
 *  （首帧默认 Output）看不到这两个按钮——该文件里也留了这条指针。这里挂载后点一下 Raw
 *  档再断言，正是真机上的操作路径。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import type { ToolCall } from '../types';
import { ToolEventSections } from './StepDetail';

const tool: ToolCall = {
  tool_call_id: 'c1',
  name: 'bash',
  args: { command: 'echo hi' },
  status: 'success',
  result: { exit_code: 0, stdout: 'hi' },
  raw_call: { type: 'tool/call', data: { x: 1 } },
  raw_result: { type: 'tool/result', data: { y: 2 } },
  started_at: '2026-09-05T10:00:00Z',
  completed_at: '2026-09-05T10:00:01Z',
} as ToolCall;

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const copyLabels = () =>
  Array.from(container.querySelectorAll<HTMLButtonElement>('.io-raw-panes button[aria-label]'))
    .map((b) => b.getAttribute('aria-label'));

describe('工具 Raw 档（B-D7）', () => {
  it('两个复制按钮的 aria-label 互不相同，且各自指向 tool/call 与 tool/result', () => {
    act(() => {
      root.render(createElement(ToolEventSections, { tool }));
    });
    const rawTab = Array.from(container.querySelectorAll<HTMLButtonElement>('.io-tab'))
      .find((b) => b.textContent?.trim() === 'Raw');
    expect(rawTab).toBeTruthy();
    act(() => rawTab!.click());

    const labels = copyLabels();
    expect(labels).toEqual(['复制 Raw（tool/call）', '复制 Raw（tool/result）']);
    expect(new Set(labels).size).toBe(labels.length);
  });
});
