/** 上下文事件（compaction / window-changed）。只列后端 `events` 已给的四种类型；
 *  未知类型原样显示机器码，不猜语义。 */

import type { ReactElement } from 'react';
import type { ContextEventPoint } from '../../lib/api';
import { formatDateTime } from '../../lib/format';
import { CONTEXT_EVENT_LABELS } from '../lib/insightsModel';

export function ContextEvents({ events }: { events: readonly ContextEventPoint[] }): ReactElement {
  return (
    <section className="ctx-insights-card">
      <header className="ctx-insights-card-head">
        <div>
          <h2>上下文事件</h2>
          <p className="ctx-insights-card-sub">压缩与窗口变更，按事件序号排列</p>
        </div>
        <span className="ctx-insights-badge is-muted">{events.length}</span>
      </header>
      {events.length === 0 ? (
        <div className="ctx-insights-empty">本会话还没有压缩或窗口变更事件</div>
      ) : (
        <ol className="ctx-insights-events">
          {events.map((event) => (
            <li key={`${event.seq}-${event.type}`} className="ctx-insights-event">
              <span className="ctx-insights-event-seq mono">#{event.seq}</span>
              <span className="ctx-insights-event-label">{CONTEXT_EVENT_LABELS[event.type] ?? event.type}</span>
              <span className="ctx-insights-event-time">{formatDateTime(event.time) ?? event.time}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
