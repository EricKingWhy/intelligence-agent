import { describe, expect, it } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent, ToolCall } from '../types';
import { eventKey } from './inspectorPanel';
import {
  barGeometry,
  deriveEventTimings,
  deriveTimelineEventKeys,
  eventMatchesFilter,
  filterCounts,
  formatAxisLabel,
  formatClock,
  seqGapMarkers,
  timelineAxis,
} from './timelineView';

const ev = (over: Partial<AgentEvent> & Pick<AgentEvent, 'type'>): AgentEvent => ({
  data: {},
  seq: null,
  session_id: 's',
  ...over,
});

describe('#1419 Timeline timing（真实数据，不编耗时）', () => {
  const call = ev({
    type: EventType.TOOL_CALL,
    data: { tool_call_id: 'tc1', tool_name: 'bash', args: { command: 'ls' } },
    seq: 1,
    run_id: 'r1',
    time: '2026-10-20T10:00:00.000Z',
  });
  const result = ev({
    type: EventType.TOOL_RESULT,
    data: { tool_call_id: 'tc1', content: '{"ok":true}' },
    seq: 2,
    run_id: 'r1',
    time: '2026-10-20T10:00:01.500Z',
  });

  it('tool/call↔tool/result：真实跨度 1500ms，失败结果映射 fail', () => {
    const timings = deriveEventTimings([call, result]);
    expect(timings.get(eventKey(call))?.durationMs).toBe(1500);
    expect(timings.get(eventKey(call))?.status).toBe('ok');
    expect(timings.get(eventKey(result))?.status).toBe('ok');
  });

  it('tool/result ok=false / cancelled：fail 与 idle 分开（中断 ≠ 失败）', () => {
    const failed = { ...result, data: { tool_call_id: 'tc1', content: '{"ok":false}' } };
    expect(deriveEventTimings([call, failed]).get(eventKey(call))?.status).toBe('fail');
    const cancelled = { ...result, data: { tool_call_id: 'tc1', content: '{"ok":false,"data":{"cancelled":true}}' } };
    expect(deriveEventTimings([call, cancelled]).get(eventKey(call))?.status).toBe('idle');
  });

  it('ToolResult 解释接受旧兼容对象，未知形状保持 idle', () => {
    const parsedObject = { ...result, data: { tool_call_id: 'tc1', content: { ok: true, data: {} } } };
    expect(deriveEventTimings([call, parsedObject]).get(eventKey(parsedObject))?.status).toBe('ok');
    const cancelledObject = {
      ...result,
      data: { tool_call_id: 'tc1', content: { ok: true, error_code: 'CANCELLED', data: { cancelled: true } } },
    };
    expect(deriveEventTimings([call, cancelledObject]).get(eventKey(cancelledObject))?.status).toBe('idle');
    for (const content of ['plain text', '{broken', '42', '"text"', 'null', '[]', { message: 'legacy' }]) {
      const unknown = { ...result, data: { tool_call_id: 'tc1', content } };
      expect(deriveEventTimings([call, unknown]).get(eventKey(unknown))?.status).toBe('idle');
    }
  });

  it('无配对结果：耗时 null（不拿相邻事件间隔冒充），运行态为 run', () => {
    const timings = deriveEventTimings([call]);
    expect(timings.get(eventKey(call))?.durationMs).toBeNull();
    expect(timings.get(eventKey(call))?.status).toBe('run');
  });

  it('model/request 优先用后端 duration_ms；缺失才回退 request-started 时间差', () => {
    const request = ev({
      type: EventType.MODEL_REQUEST,
      data: { outcome: 'completed', duration_ms: 2400 },
      seq: 1,
      time: '2026-10-20T10:00:02.000Z',
    });
    const onlyDone = deriveEventTimings([request]).get(eventKey(request))!;
    expect(onlyDone.durationMs).toBe(2400);
    // P1-2：没有 request-started 时，起点 = 落盘时刻 − duration_ms。
    expect(onlyDone.start).toBe(Date.parse('2026-10-20T10:00:02.000Z') - 2400);

    const started = ev({ type: EventType.MODEL_REQUEST_STARTED, data: { request_id: 'q1' }, seq: 1, time: '2026-10-20T10:00:00.000Z' });
    const done = ev({ type: EventType.MODEL_REQUEST, data: { request_id: 'q1', outcome: 'completed' }, seq: 2, time: '2026-10-20T10:00:01.250Z' });
    const paired = deriveEventTimings([started, done]).get(eventKey(done))!;
    expect(paired.durationMs).toBe(1250);
    // P1-2 复现夹具：started(t=0) + request(t=2.4s, duration=2400) ⇒ 条起点 0，不是 2400。
    expect(paired.start).toBe(Date.parse('2026-10-20T10:00:00.000Z'));
  });

  it('P1-2 复现：model/request 条起点回到请求开始（不是完成时刻）', () => {
    const started = ev({
      type: EventType.MODEL_REQUEST_STARTED,
      data: { request_id: 'q1' },
      seq: 1,
      run_id: 'r1',
      time: '2026-10-20T10:00:00.000Z',
    });
    const done = ev({
      type: EventType.MODEL_REQUEST,
      data: { request_id: 'q1', outcome: 'completed', duration_ms: 2400 },
      seq: 2,
      run_id: 'r1',
      time: '2026-10-20T10:00:02.400Z',
    });
    const completed = ev({ type: EventType.RUN_COMPLETED, data: {}, seq: 3, run_id: 'r1', time: '2026-10-20T10:00:05.000Z' });
    const timings = deriveEventTimings([started, done, completed]);
    const timing = timings.get(eventKey(done))!;
    expect(timing.start).toBe(Date.parse('2026-10-20T10:00:00.000Z'));
    expect(timing.durationMs).toBe(2400);
    const axis = timelineAxis(timings)!;
    const bar = barGeometry(timing, axis)!;
    expect(bar.left).toBe(0);
    expect(bar.width).toBe(48); // 2400 / 5000
  });

  it('run/started↔run/completed：run 跨度可算；delegation 同理', () => {
    const runStart = ev({ type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', time: '2026-10-20T10:00:00.000Z' });
    const runEnd = ev({ type: EventType.RUN_COMPLETED, data: {}, seq: 2, run_id: 'r1', time: '2026-10-20T10:00:05.000Z' });
    expect(deriveEventTimings([runStart, runEnd]).get(eventKey(runStart))?.durationMs).toBe(5000);

    const dStart = ev({ type: EventType.AGENT_DELEGATION_STARTED, data: { child_session_id: 'c1' }, seq: 3, time: '2026-10-20T10:00:06.000Z' });
    const dEnd = ev({ type: EventType.AGENT_DELEGATION_FINISHED, data: { child_session_id: 'c1' }, seq: 4, time: '2026-10-20T10:00:09.000Z' });
    expect(deriveEventTimings([dStart, dEnd]).get(eventKey(dStart))?.durationMs).toBe(3000);
  });

  it('SSE 无 time：工具时间从投影的 started_at/completed_at 回退（真实时钟，不是编的）', () => {
    const noTimeCall = ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 'tc9', tool_name: 'read' }, seq: 1 });
    const noTimeResult = ev({ type: EventType.TOOL_RESULT, data: { tool_call_id: 'tc9', content: '{"ok":true}' }, seq: 2 });
    const tool: ToolCall = {
      tool_call_id: 'tc9',
      name: 'read',
      args: {},
      status: 'success',
      started_at: '2026-10-20T10:00:00.000Z',
      completed_at: '2026-10-20T10:00:00.400Z',
    };
    const timing = deriveEventTimings([noTimeCall, noTimeResult], [tool]).get(eventKey(noTimeCall));
    expect(timing?.start).toBe(Date.parse('2026-10-20T10:00:00.000Z'));
    expect(timing?.durationMs).toBe(400);
  });

  it('timelineAxis / barGeometry：短事件仍有最小宽度；无时间返回 null', () => {
    const a = ev({ type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1', time: '2026-10-20T10:00:00.000Z' });
    const b = ev({ type: EventType.USER_MESSAGE, data: {}, seq: 2, run_id: 'r1', time: '2026-10-20T10:00:10.000Z' });
    const timings = deriveEventTimings([a, b]);
    const axis = timelineAxis(timings)!;
    expect(axis.maxMs).toBe(10_000);
    const bar = barGeometry(timings.get(eventKey(b))!, axis)!;
    expect(bar.left).toBe(100);
    expect(bar.width).toBe(0.35);
    expect(bar.point).toBe(true);
    expect(timelineAxis(new Map())).toBeNull();
  });
});

describe('#1419 筛选计数 / 缺口 / 格式', () => {
  const events: AgentEvent[] = [
    ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' }, seq: 1, time: '2026-10-20T10:00:00.000Z' }),
    ev({ type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: '{"ok":false}' }, seq: 2, time: '2026-10-20T10:00:01.000Z' }),
    ev({ type: EventType.MODEL_REQUEST, data: { outcome: 'completed' }, seq: 3, time: '2026-10-20T10:00:02.000Z' }),
    ev({ type: EventType.RUN_FAILED, data: {}, seq: 6, run_id: 'r1', time: '2026-10-20T10:00:03.000Z' }),
  ];

  it('计数 = 当前时间线全部事件；fail 与状态判据同源', () => {
    const timings = deriveEventTimings(events);
    expect(filterCounts(events, timings)).toEqual({ all: 4, tool: 2, model: 1, fail: 3 });
  });

  it('eventMatchesFilter：tool/model/fail/all', () => {
    const timings = deriveEventTimings(events);
    expect(events.filter((e) => eventMatchesFilter(e, 'tool', timings.get(eventKey(e))))).toHaveLength(2);
    expect(events.filter((e) => eventMatchesFilter(e, 'model', timings.get(eventKey(e))))).toHaveLength(1);
    expect(events.filter((e) => eventMatchesFilter(e, 'fail', timings.get(eventKey(e))))).toHaveLength(3);
    expect(events.filter((e) => eventMatchesFilter(e, 'all', timings.get(eventKey(e))))).toHaveLength(4);
  });

  it('seqGapMarkers 保留插入位置与缺失条数（3→6 = 缺 2 条）', () => {
    expect(seqGapMarkers(events)).toEqual([{ afterIndex: 2, from: 3, to: 6, missing: 2 }]);
  });

  it('P2-1：seq-less 事件在 Timeline 层有稳定区分键，eventKey 本身不变', () => {
    const a = ev({ type: EventType.MODEL_STARTED, data: {}, seq: null, run_id: 'r1', time: '2026-10-20T10:00:00.000Z' });
    const b = ev({ type: EventType.MODEL_STARTED, data: {}, seq: null, run_id: 'r1', time: '2026-10-20T10:00:01.000Z' });
    const c = ev({ type: EventType.MODEL_STARTED, data: {}, seq: null, run_id: 'r1', time: '2026-10-20T10:00:02.000Z' });
    expect(eventKey(a)).toBe(eventKey(b));
    const keys = deriveTimelineEventKeys([a, b, c]);
    expect(new Set(keys.values()).size).toBe(3);
    expect(keys.get(a)).toBe(eventKey(a));
    expect(keys.get(b)).toBe(`${eventKey(a)}#1`);
    expect(keys.get(c)).toBe(`${eventKey(a)}#2`);
    // 唯一键的事件保持原键（既有选中/展开语义不变）。
    const unique = ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1' }, seq: 7, run_id: 'r1' });
    expect(deriveTimelineEventKeys([unique]).get(unique)).toBe(eventKey(unique));
  });

  it('formatClock 本地时钟 / formatAxisLabel 固定单位', () => {
    expect(formatClock('not-a-date')).toBeNull();
    expect(formatClock('2026-10-20T10:03:04.000Z')).toMatch(/^\d{2}:\d{2}:\d{2}$/);
    expect(formatAxisLabel(0)).toBe('0ms');
    expect(formatAxisLabel(1500)).toBe('1.5s');
    expect(formatAxisLabel(15_000)).toBe('15s');
    expect(formatAxisLabel(90_000)).toBe('1m30s');
  });
});
