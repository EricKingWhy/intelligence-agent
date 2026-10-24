import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { ToolCard } from '../components/ToolCard';
import { EventType } from '../types';
import type { AgentEvent } from '../types';
import { allTools, projectHistory } from './projection';
import { eventKey } from './inspectorPanel';
import { deriveEventTimings, eventMatchesFilter, filterCounts } from './timelineView';
import { timelineHeadline } from './timelineHeadline';

const SESSION = 'tool-result-semantics';
const RUN = 'run-1';
const CANCELLED_CONTENT = '{"ok":false,"message":"cancelled","data":{"cancelled":true}}';

function event(
  type: string,
  seq: number,
  data: Record<string, unknown> = {},
  step_id: number | null = null,
): AgentEvent {
  return {
    type,
    data,
    seq,
    session_id: SESSION,
    run_id: RUN,
    step_id,
    time: `2026-10-20T10:00:0${seq}.000Z`,
  } as AgentEvent;
}

describe('ToolResult interpretation across projection, card, and Timeline', () => {
  it('keeps one cancellation fixture stopped and out of the failure headline/filter', () => {
    const events = [
      event(EventType.RUN_STARTED, 1),
      event(EventType.TOOL_CALL, 2, {
        tool_call_id: 'tc-cancel',
        tool_name: 'bash',
        args: { command: 'sleep 1' },
      }, 1),
      event(EventType.TOOL_OUTPUT_DELTA, 3, {
        tool_call_id: 'tc-cancel',
        channel: 'stdout',
        delta: 'partial output',
      }, 1),
      event(EventType.TOOL_RESULT, 4, { tool_call_id: 'tc-cancel', content: CANCELLED_CONTENT }, 1),
    ];
    const conversation = projectHistory(SESSION, events);
    const tool = allTools(conversation)[0];
    const timings = deriveEventTimings(conversation.events, [tool]);
    const result = conversation.events.find((item) => item.type === EventType.TOOL_RESULT)!;
    const call = conversation.events.find((item) => item.type === EventType.TOOL_CALL)!;
    const card = renderToString(createElement(ToolCard, { tool, density: 'balanced' }));

    expect(tool.status).toBe('stopped');
    expect(tool.error).toBeUndefined();
    expect(tool.output?.map((chunk) => chunk.text)).toEqual(['partial output']);
    expect(tool.raw_result).toEqual(result);
    expect(timings.get(eventKey(call))?.status).toBe('idle');
    expect(timings.get(eventKey(result))?.status).toBe('idle');
    expect(timelineHeadline(result, tool).title).toBe('跑 sleep 1 · 已中断');
    expect(eventMatchesFilter(result, 'fail', timings.get(eventKey(result)))).toBe(false);
    expect(filterCounts(conversation.events, timings).fail).toBe(0);
    expect(card).toContain('act-status-stopped');
    expect(card).not.toContain('act-error-line');
  });

  it('keeps projection fallbacks for legacy and malformed inputs', () => {
    const cases: { content: unknown; status: string; result: unknown }[] = [
      { content: '{"ok":true,"data":{"exit_code":0}}', status: 'success', result: { exit_code: 0 } },
      { content: '{"ok":false}', status: 'failed', result: '{"ok":false}' },
      { content: '{"message":"legacy"}', status: 'failed', result: 'legacy' },
      { content: 'plain text', status: 'failed', result: 'plain text' },
      { content: '{broken', status: 'failed', result: '{broken' },
      { content: '42', status: 'failed', result: '42' },
      { content: '"text"', status: 'failed', result: '"text"' },
      { content: 'null', status: 'failed', result: 'null' },
      { content: '[]', status: 'failed', result: '[]' },
      { content: { ok: true }, status: 'failed', result: { ok: true } },
    ];

    for (const [index, testCase] of cases.entries()) {
      const events = [
        event(EventType.TOOL_CALL, index * 2 + 1, {
          tool_call_id: `tc-${index}`,
          tool_name: 'bash',
          args: { command: 'true' },
        }, 1),
        event(EventType.TOOL_RESULT, index * 2 + 2, {
          tool_call_id: `tc-${index}`,
          content: testCase.content,
        }, 1),
      ];
      const tool = allTools(projectHistory(SESSION, events))[0];
      expect(tool.status, `case ${index}`).toBe(testCase.status);
      expect(tool.result, `case ${index}`).toEqual(testCase.result);
      expect(tool.error, `case ${index}`).toBeUndefined();
    }
  });

  it('cancellation wins over ok=true and keeps raw result intact', () => {
    const result = event(EventType.TOOL_RESULT, 3, {
      tool_call_id: 'tc-cancel-ok',
      content: JSON.stringify({
        ok: true,
        error_code: 'COMMAND_CANCELLED',
        message: 'cancelled',
        data: { cancelled: true },
      }),
    }, 1);
    const conversation = projectHistory(SESSION, [
      event(EventType.TOOL_CALL, 2, {
        tool_call_id: 'tc-cancel-ok',
        tool_name: 'bash',
        args: { command: 'sleep 1' },
      }, 1),
      result,
    ]);
    const tool = allTools(conversation)[0];

    expect(tool.status).toBe('stopped');
    expect(tool.error).toBeUndefined();
    expect(tool.raw_result).toEqual(result);
  });

  it('interprets an orphan cancellation result without inventing a ToolCall', () => {
    const result = event(EventType.TOOL_RESULT, 3, {
      tool_call_id: 'missing-call',
      content: CANCELLED_CONTENT,
    }, 1);
    const conversation = projectHistory(SESSION, [result]);
    const timings = deriveEventTimings(conversation.events, allTools(conversation));

    expect(allTools(conversation)).toHaveLength(0);
    expect(timelineHeadline(result).title).toBe('工具返回 · 已中断');
    expect(timings.get(eventKey(result))?.status).toBe('idle');
    expect(filterCounts(conversation.events, timings).fail).toBe(0);
  });
});
