import { describe, expect, it } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent, ToolCall } from '../types';
import { eventKey } from './inspectorPanel';
import { deriveTimelineModel, deriveTimelineView } from './timelineView';

const ev = (over: Partial<AgentEvent> & Pick<AgentEvent, 'type'>): AgentEvent => ({
  data: {},
  seq: null,
  session_id: 's',
  ...over,
});

const events: AgentEvent[] = [
  ev({ type: EventType.SESSION_STARTED, seq: 1, time: '2026-10-20T09:59:59.000Z' }),
  ev({ type: EventType.RUN_STARTED, seq: 2, run_id: 'r1', time: '2026-10-20T10:00:00.000Z' }),
  ev({
    type: EventType.TOOL_CALL,
    data: { tool_call_id: 'tc1', tool_name: 'bash', args: { command: 'false' } },
    seq: 3,
    run_id: 'r1',
    time: '2026-10-20T10:00:01.000Z',
  }),
  ev({
    type: EventType.TOOL_RESULT,
    data: { tool_call_id: 'tc1', content: '{"ok":false}' },
    seq: 4,
    run_id: 'r1',
    time: '2026-10-20T10:00:04.000Z',
  }),
  ev({ type: EventType.MODEL_STARTED, seq: null, run_id: 'r1', time: '2026-10-20T10:00:04.500Z' }),
  ev({ type: EventType.MODEL_STARTED, seq: null, run_id: 'r1', time: '2026-10-20T10:00:04.750Z' }),
  ev({
    type: EventType.MODEL_REQUEST,
    data: { outcome: 'completed', duration_ms: 1000 },
    seq: 7,
    run_id: 'r1',
    time: '2026-10-20T10:00:06.000Z',
  }),
  ev({ type: EventType.RUN_FAILED, data: { reason: 'error' }, seq: 9, run_id: 'r1', time: '2026-10-20T10:00:08.000Z' }),
];

const tool: ToolCall = {
  tool_call_id: 'tc1',
  name: 'bash',
  args: { command: 'false' },
  status: 'failed',
  started_at: '2026-10-20T10:00:01.000Z',
  completed_at: '2026-10-20T10:00:04.000Z',
};

describe('composed Timeline view', () => {
  it('derives one base model and shares its ordered, pre-window rows across filters', () => {
    const model = deriveTimelineModel(events, [tool]);

    expect(model.rows.map(({ rawIndex }) => rawIndex)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(model.rows.map(({ rowKey }) => rowKey)).toEqual([
      eventKey(events[0]),
      eventKey(events[1]),
      eventKey(events[2]),
      eventKey(events[3]),
      eventKey(events[4]),
      `${eventKey(events[4])}#1`,
      eventKey(events[6]),
      eventKey(events[7]),
    ]);
    expect(new Set(model.rows.map(({ rowKey }) => rowKey)).size).toBe(events.length);
    expect(eventKey(events[4])).toBe(eventKey(events[5]));
    expect(model.rows[2].timing).toEqual({
      start: Date.parse('2026-10-20T10:00:01.000Z'),
      durationMs: 3000,
      status: 'fail',
    });
    expect(model.rows[5].timing).toEqual({
      start: Date.parse('2026-10-20T10:00:04.750Z'),
      durationMs: null,
      status: 'idle',
    });
    expect(model.counts).toEqual({ all: 8, tool: 2, model: 3, fail: 3 });
    expect(model.axis).toEqual({
      start: Date.parse('2026-10-20T09:59:59.000Z'),
      end: Date.parse('2026-10-20T10:00:08.000Z'),
      maxMs: 9000,
    });
    expect(model.runGroups).toEqual([
      { runId: null, ordinal: 0, status: 'running', count: 1, start: 0 },
      { runId: 'r1', ordinal: 1, status: 'failed', count: 7, start: 1 },
    ]);
    expect([...model.gapByIndex.values()]).toEqual([
      { afterIndex: 3, from: 4, to: 7, missing: 2 },
      { afterIndex: 6, from: 7, to: 9, missing: 1 },
    ]);
    expect(model.callById.get('tc1')).toBe(tool);

    expect(deriveTimelineView(model, 'tool').rows.map(({ rawIndex }) => rawIndex)).toEqual([2, 3]);
    expect(deriveTimelineView(model, 'model').rows.map(({ rawIndex }) => rawIndex)).toEqual([4, 5, 6]);
    expect(deriveTimelineView(model, 'fail').rows.map(({ rawIndex }) => rawIndex)).toEqual([2, 3, 7]);
    expect(deriveTimelineView(model, 'all').rows.map(({ rawIndex }) => rawIndex)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
  });
});
