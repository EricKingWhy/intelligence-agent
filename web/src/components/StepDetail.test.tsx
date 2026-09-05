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
