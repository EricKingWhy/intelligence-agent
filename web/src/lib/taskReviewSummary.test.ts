import { describe, expect, it } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent } from '../types';
import { summarizeTaskRuns } from './taskReviewSummary';

function event(
  type: string,
  runId: string,
  options: { time?: string; reason?: string } = {},
): AgentEvent {
  return {
    type,
    data: options.reason ? { reason: options.reason } : {},
    run_id: runId,
    seq: null,
    ...(options.time ? { time: options.time } : {}),
  };
}

describe('summarizeTaskRuns', () => {
  it('counts distinct failed and interrupted runs but excludes cancellations', () => {
    const summary = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'done'),
      event(EventType.RUN_COMPLETED, 'done'),
      event(EventType.RUN_STARTED, 'failed'),
      event(EventType.RUN_FAILED, 'failed'),
      event(EventType.RUN_STARTED, 'interrupted'),
      event(EventType.RUN_INTERRUPTED, 'interrupted'),
      event(EventType.RUN_STARTED, 'cancelled'),
      event(EventType.RUN_FAILED, 'cancelled', { reason: 'cancelled' }),
      event(EventType.MODEL_DELTA, 'failed'),
    ]);

    expect(summary).toEqual({
      runCount: 4,
      incompleteCount: 2,
      hasTerminalRun: true,
      hasRunningRun: false,
      emphasizeDetails: true,
      latestTerminalRunId: 'cancelled',
    });
  });

  it('reports the most recent terminal run id, skipping an open run', () => {
    const summary = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'done'),
      event(EventType.RUN_COMPLETED, 'done'),
      event(EventType.RUN_STARTED, 'active'),
    ]);

    expect(summary.latestTerminalRunId).toBe('done');
  });

  it('keys the most recent terminal run by terminal event order, not first appearance', () => {
    // r1 先起、后完；r2 后起、先完。首次出现序的「最后一个」是 r2，终态序的是 r1。
    const summary = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'r1'),
      event(EventType.RUN_STARTED, 'r2'),
      event(EventType.RUN_COMPLETED, 'r2'),
      event(EventType.RUN_COMPLETED, 'r1'),
    ]);
    expect(summary.latestTerminalRunId).toBe('r1');
  });

  it('keeps the summary unavailable while any distinct run is still open', () => {
    const summary = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'done'),
      event(EventType.RUN_COMPLETED, 'done'),
      event(EventType.RUN_STARTED, 'active'),
    ]);

    expect(summary.hasTerminalRun).toBe(true);
    expect(summary.hasRunningRun).toBe(true);
  });

  it('emphasizes after three distinct runs', () => {
    const summary = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'one'),
      event(EventType.RUN_COMPLETED, 'one'),
      event(EventType.RUN_STARTED, 'two'),
      event(EventType.RUN_COMPLETED, 'two'),
      event(EventType.RUN_STARTED, 'three'),
      event(EventType.RUN_COMPLETED, 'three'),
    ]);

    expect(summary.emphasizeDetails).toBe(true);
  });

  it('emphasizes a run longer than ten minutes using event timestamps', () => {
    const summary = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'long', { time: '2026-10-16T00:00:00.000Z' }),
      event(EventType.RUN_COMPLETED, 'long', { time: '2026-10-16T00:10:01.000Z' }),
    ]);

    expect(summary.emphasizeDetails).toBe(true);
  });

  it('does not infer long duration without valid timestamps or at the exact threshold', () => {
    const noTimes = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'unknown'),
      event(EventType.RUN_COMPLETED, 'unknown'),
    ]);
    const exactThreshold = summarizeTaskRuns([
      event(EventType.RUN_STARTED, 'exact', { time: '2026-10-16T00:00:00.000Z' }),
      event(EventType.RUN_COMPLETED, 'exact', { time: '2026-10-16T00:10:00.000Z' }),
    ]);

    expect(noTimes.emphasizeDetails).toBe(false);
    expect(exactThreshold.emphasizeDetails).toBe(false);
  });
});
