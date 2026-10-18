// @vitest-environment jsdom
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, it } from 'vitest';
import { EventType, type AgentEvent } from '../types';
import { changedFiles } from '../lib/changedFiles';
import { ChangesPanel } from './ChangesPanel';

describe('ChangesPanel event memoization', () => {
  it('recomputes the review summary when events are appended in place', () => {
    const events: AgentEvent[] = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1' },
    ];
    const host = document.createElement('div');
    const root = createRoot(host);
    const renderAtVersion = (eventsVersion: number) => {
      const props = {
        ...changedFiles([]),
        events,
        eventsVersion,
      };
      act(() => root.render(createElement(ChangesPanel, props)));
    };

    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    try {
      renderAtVersion(1);
      expect(host.querySelector('.task-review-summary')).toBeNull();

      events.push({ type: EventType.RUN_COMPLETED, data: {}, seq: 2, run_id: 'r1' });
      renderAtVersion(2);

      expect(host.querySelector('.task-review-summary')).not.toBeNull();
    } finally {
      act(() => root.unmount());
      delete (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
    }
  });

  it('mark-as-read de-emphasizes the summary for that run only (#1325 S2)', () => {
    const events: AgentEvent[] = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1' },
      { type: EventType.RUN_FAILED, data: { reason: 'error' }, seq: 2, run_id: 'r1' },
    ];
    const host = document.createElement('div');
    const root = createRoot(host);
    const renderAtVersion = (version: number) => {
      act(() => root.render(createElement(ChangesPanel, {
        ...changedFiles([]),
        events,
        eventsVersion: version,
      })));
    };

    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    try {
      renderAtVersion(2);
      expect(host.querySelector('.task-review-summary.is-warning')).not.toBeNull();

      const markRead = [...host.querySelectorAll('button')].find(
        (b) => b.textContent === '标记已读',
      ) as HTMLButtonElement;
      expect(markRead).toBeDefined();
      act(() => markRead.click());

      // 同一 run：退出警示强调，弱入口收起，回执可见。
      expect(host.querySelector('.task-review-summary.is-warning')).toBeNull();
      expect(host.querySelector('.task-review-summary')?.textContent).toContain(
        '已标记为已读，不会阻断后续操作',
      );
      expect([...host.querySelectorAll('button')].some((b) => b.textContent === '标记已读')).toBe(false);

      // 新 run 终态：提醒重新出现（作用域是 run，不是整个会话）。
      events.push({ type: EventType.RUN_STARTED, data: {}, seq: 3, run_id: 'r2' });
      events.push({ type: EventType.RUN_FAILED, data: { reason: 'error' }, seq: 4, run_id: 'r2' });
      renderAtVersion(4);
      expect(host.querySelector('.task-review-summary.is-warning')).not.toBeNull();
      expect([...host.querySelectorAll('button')].some((b) => b.textContent === '标记已读')).toBe(true);
    } finally {
      act(() => root.unmount());
      delete (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
    }
  });

  it('recomputes the review summary when the selected session changes at the same event version', () => {
    const completedEvents: AgentEvent[] = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r1' },
      { type: EventType.RUN_COMPLETED, data: {}, seq: 2, run_id: 'r1' },
    ];
    const runningEvents: AgentEvent[] = [
      { type: EventType.RUN_STARTED, data: {}, seq: 1, run_id: 'r2' },
      { type: EventType.RUN_STARTED, data: {}, seq: 2, run_id: 'r3' },
    ];
    const host = document.createElement('div');
    const root = createRoot(host);
    const renderSession = (sessionId: string, events: AgentEvent[]) => {
      act(() => root.render(createElement(ChangesPanel, {
        ...changedFiles([]),
        sessionId,
        events,
        eventsVersion: 2,
      })));
    };

    (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
    try {
      renderSession('s1', completedEvents);
      expect(host.querySelector('.task-review-summary')).not.toBeNull();

      renderSession('s2', runningEvents);
      expect(host.querySelector('.task-review-summary')).toBeNull();
    } finally {
      act(() => root.unmount());
      delete (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT;
    }
  });
});
