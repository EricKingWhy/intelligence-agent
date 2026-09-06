/** Timeline 尾窗裁剪（P1-4）的渲染行为契约——SSR 测试，无需 testing-library。
 *  真相全量在 conversation.events；视图只渲染窗口（不变量 #22：裁的是视图不是数据）。
 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { EventType } from '../types';
import type { ConversationState } from '../types';
import { initConversation, applyEvent } from '../lib/projection';
import { TimelineTab, TIMELINE_WINDOW_DEFAULT, TIMELINE_WINDOW_STEP } from './StepDetail';

function bigConversation(n: number): ConversationState {
  let s = initConversation('b');
  for (let i = 0; i < n; i++) {
    s = applyEvent(s, { type: EventType.MODEL_DELTA, data: { delta: 'x' }, seq: i, run_id: 'r', step_id: 1, session_id: 'b' });
  }
  return s;
}

const noop = () => {};
const rowCount = (html: string) => (html.match(/timeline-row/g) || []).length;
const renderTab = (conv: ConversationState) =>
  // SSR 会在插值文本节点间插入 <!-- --> 分隔注释——断言前剥离，避免误报
  renderToString(createElement(TimelineTab, { conversation: conv, onFocusEvent: noop })).replaceAll('<!-- -->', '');

describe('TimelineTab 尾窗裁剪', () => {
  it('小会话（≤ 窗口）：全量渲染，无折叠条', () => {
    const html = renderTab(bigConversation(50));
    expect(rowCount(html)).toBe(50);
    expect(html).not.toContain('timeline-window-bar');
  });

  it(`大会话（${TIMELINE_WINDOW_DEFAULT * 3} 事件）：只渲染最近 ${TIMELINE_WINDOW_DEFAULT} 行 + 折叠条`, () => {
    const total = TIMELINE_WINDOW_DEFAULT * 3;
    const html = renderTab(bigConversation(total));
    expect(rowCount(html)).toBe(TIMELINE_WINDOW_DEFAULT);
    expect(html).toContain('timeline-window-bar');
    expect(html).toContain(`共 ${total} 条`);
    // 窗口是"最近"而非"最早"——最后一行必须在
    expect(html).toContain(`>${total - 1}<`);
    // 最早一行被折叠
    expect(html).not.toContain('>--1<');
  });

  it('窗口行 key 用全局序号：扩展窗口后既有行 identity 稳定（append-only 契约）', () => {
    // key = hidden + i：对 2000 事件窗口 200，最后可见行全局序号 1999
    const html = renderTab(bigConversation(2000));
    expect(html).toContain(`>${2000 - 1}<`);
    expect(rowCount(html)).toBe(TIMELINE_WINDOW_DEFAULT);
  });

  it(`步长常量：${TIMELINE_WINDOW_DEFAULT} / ${TIMELINE_WINDOW_STEP}`, () => {
    expect(TIMELINE_WINDOW_DEFAULT).toBe(200);
    expect(TIMELINE_WINDOW_STEP).toBe(500);
  });
});

// ── C2：ToolEventSections Input/Output/Raw 标签条化 ──

import { ToolEventSections } from './StepDetail';
import type { ToolCall } from '../types';

const toolBase: ToolCall = {
  tool_call_id: 'c1',
  name: 'bash',
  args: { command: 'echo hi' },
  status: 'success',
  result: { exit_code: 0, stdout: 'hi' },
  started_at: '2026-09-05T10:00:00Z',
  completed_at: '2026-09-05T10:00:01Z',
};

const renderTool = (tool: ToolCall) =>
  renderToString(createElement(ToolEventSections, { tool })).replaceAll('<!-- -->', '');

describe('ToolEventSections 标签条（C2）', () => {
  it('三段堆叠收敛为 Input/Output 标签条；有 result 时默认 Output 选中', () => {
    const html = renderTool(toolBase);
    expect(html).toContain('io-tabs');
    expect(html).toContain('>Input<');
    expect(html).toContain('>Output<');
    // 默认 Output 选中（aria-selected）
    expect(html).toMatch(/aria-selected="true"[^>]*>Output</);
    // Output 面板内容在（exit_code 键来自 result 对象树）
    expect(html).toContain('exit_code');
    // Input 面板默认不渲染其 args 内容（command 是 args 独有键）
    expect(html).not.toContain('>command<');
  });

  it('无 result：默认 Input 选中且无 Output 标签', () => {
    const html = renderTool({ ...toolBase, result: undefined, status: 'running' });
    expect(html).toContain('>Input<');
    expect(html).not.toContain('>Output<');
    expect(html).toContain('>command<');
  });

  it('有 raw_call/raw_result 才出现 Raw 标签；raw 面板渲染原始事件树', () => {
    const noRaw = renderTool(toolBase);
    expect(noRaw).not.toContain('>Raw<');
    const withRaw = renderTool({
      ...toolBase,
      raw_call: { type: 'tool/call', data: { x: 1 } },
      raw_result: { type: 'tool/result', data: { y: 2 } },
    } as ToolCall);
    expect(withRaw).toContain('>Raw<');
  });
});

// ── C4：Timeline 行 hover 时间戳浮层 ──

import { formatEventTooltip } from './StepDetail';

describe('formatEventTooltip（C4）', () => {
  it('time + step 齐全：两行（完整时间戳含毫秒 + step）', () => {
    const lines = formatEventTooltip({
      type: EventType.TOOL_CALL, data: {}, seq: 3, run_id: 'r',
      step_id: 9, session_id: 's', time: '2026-09-05T13:17:06.288+08:00',
    } as Parameters<typeof formatEventTooltip>[0]);
    expect(lines).toHaveLength(2);
    expect(lines[0]).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}$/);
    expect(lines[1]).toBe('step 9');
  });

  it('time 缺失：只保留 step 行', () => {
    const lines = formatEventTooltip({
      type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r', step_id: 2, session_id: 's', time: null,
    } as Parameters<typeof formatEventTooltip>[0]);
    expect(lines).toEqual(['step 2']);
  });

  it('非法 time 字符串：不产出时间行', () => {
    const lines = formatEventTooltip({
      type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r', step_id: null, session_id: 's', time: 'not-a-date',
    } as Parameters<typeof formatEventTooltip>[0]);
    expect(lines).toEqual([]);
  });

  it('step_id 与 time 都缺：空数组（调用方不渲染浮层）', () => {
    const lines = formatEventTooltip({
      type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r', step_id: null, session_id: 's', time: null,
    } as Parameters<typeof formatEventTooltip>[0]);
    expect(lines).toEqual([]);
  });

  it('step_id === 0：按合法数值渲染 step 行（0 不是哨兵，null 才是）', () => {
    const lines = formatEventTooltip({
      type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r', step_id: 0, session_id: 's', time: null,
    } as Parameters<typeof formatEventTooltip>[0]);
    expect(lines).toEqual(['step 0']);
  });
});
