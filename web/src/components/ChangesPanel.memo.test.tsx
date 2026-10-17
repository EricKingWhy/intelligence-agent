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
