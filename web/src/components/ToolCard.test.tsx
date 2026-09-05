/** ToolCard 密度档渲染契约——SSR 测试（C3）。
 *  锁定：detailed/raw 档内联明细的字段标签（Input/Output）、
 *  balanced 档无内联明细、compact 档无图标与全宽参数。
 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { ToolCard } from './ToolCard';
import type { ToolCall } from '../types';

const tool: ToolCall = {
  tool_call_id: 'c1',
  name: 'read',
  args: { path: 'README.md' },
  status: 'success',
  result: { content: 'x', total_lines: 1 },
  started_at: '2026-09-05T10:00:00Z',
  completed_at: '2026-09-05T10:00:01Z',
};

const renderCard = (density: 'compact' | 'balanced' | 'detailed' | 'raw') =>
  renderToString(createElement(ToolCard, { tool, density })).replaceAll('<!-- -->', '');

describe('ToolCard 内联明细字段标签（C3）', () => {
  it('detailed 档：Input/Output 微标签 + args/result 内容', () => {
    const html = renderCard('detailed');
    expect(html).toContain('act-detail-inline');
    expect(html).toContain('act-field-label');
    expect(html).toContain('>Input<');
    expect(html).toContain('>Output<');
    expect(html).toContain('act-detail-args');
    expect(html).toContain('act-detail-result');
  });

  it('raw 档：含原始事件区标签', () => {
    const t: ToolCall = {
      ...tool,
      raw_call: { type: 'tool/call' },
      raw_result: { type: 'tool/result' },
    } as ToolCall;
    const html = renderToString(createElement(ToolCard, { tool: t, density: 'raw' })).replaceAll('<!-- -->', '');
    expect(html).toContain('act-raw-label');
    expect(html).toContain('tool/call 原始事件');
  });

  it('balanced 档：无内联明细', () => {
    const html = renderCard('balanced');
    expect(html).not.toContain('act-detail-inline');
  });

  it('compact 档：无图标、无全宽参数（仅截短参数）', () => {
    const html = renderCard('compact');
    expect(html).not.toContain('act-icon');
    expect(html).toContain('act-args-compact');
  });
});
