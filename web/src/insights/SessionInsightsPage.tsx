/** 单会话洞察页（#1420）：上下文 / Fleet 两个子页。（ADAPT：dsh-context
 *  `src/client/components/contextView.tsx` @1bc481c，Apache-2.0；上下文 / Fleet
 *  子页拆分与「构成 → 趋势 → 事件」顺序沿用，布局与数据流是本仓新代码。）
 *
 *  上下文数据来自 `GET /api/sessions/{id}/context-timeline`（window + requests +
 *  events 一次取齐）；Fleet 来自 `GET /api/sessions/{id}/fleet`，独立请求、独立
 *  降级——一边失败不拖垮另一边。页面外壳先渲染，失败只落在内容区。 */

import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactElement,
} from 'react';
import type {
  ContextGranularity,
  ContextMode,
  ContextTimeline,
  FleetPayload,
} from '../lib/api';
import { getContextTimeline, getSessionFleet } from '../lib/api';
import { tabKeyTarget } from '../lib/capabilities';
import { attachEventMarkers } from './lib/insightsModel';
import { ContextComposition } from './components/ContextComposition';
import { ContextEvents } from './components/ContextEvents';
import { FleetView } from './components/FleetView';
import { InsightsError, InsightsSkeleton } from './components/InsightsStatus';
import { TrendChart } from './components/TrendChart';
import './insights.css';

export interface SessionInsightsPageProps {
  sessionId: string;
  onClose: () => void;
  onOpenSession: (sessionId: string) => void;
}

/** 子页登记表：key 同时用于 tab / panel 的稳定 id，方向键按这张表的顺序走。 */
const SESSION_TABS = [
  { key: 'context', label: '上下文' },
  { key: 'fleet', label: 'Fleet' },
] as const;

type Tab = (typeof SESSION_TABS)[number]['key'];

export function SessionInsightsPage({
  sessionId,
  onClose,
  onOpenSession,
}: SessionInsightsPageProps): ReactElement {
  const [tab, setTab] = useState<Tab>('context');
  const [granularity, setGranularity] = useState<ContextGranularity>('step');
  const [mode, setMode] = useState<ContextMode>('total');
  const [timeline, setTimeline] = useState<ContextTimeline | null>(null);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const [timelineLoading, setTimelineLoading] = useState(false);
  const [timelineReload, setTimelineReload] = useState(0);
  const [pinnedSeq, setPinnedSeq] = useState<number | null>(null);
  const [fleet, setFleet] = useState<FleetPayload | null>(null);
  const [fleetError, setFleetError] = useState<string | null>(null);
  const [fleetLoading, setFleetLoading] = useState(false);
  const [fleetReload, setFleetReload] = useState(0);
  const cacheRef = useRef(new Map<string, ContextTimeline>());
  const backRef = useRef<HTMLButtonElement>(null);
  const tabRefs = useRef<(HTMLButtonElement | null)[]>([]);

  // 「更多工具」菜单关闭时 Radix 会把焦点还给触发器；TopBar 已在那一次阻止归还，
  // 这里再把焦点落进页面。延后一拍是仓库既有浮层的做法（见 TopBar 令牌面板）：
  // 菜单卸载 / 页面挂载的 effect 顺序不由本组件决定。
  useEffect(() => {
    const timer = window.setTimeout(() => backRef.current?.focus(), 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  /** WAI-ARIA tabs 的 roving focus：方向键 / Home / End 换页并移动焦点。 */
  const handleTabKeyDown = (event: ReactKeyboardEvent<HTMLButtonElement>) => {
    const target = tabKeyTarget(SESSION_TABS, tab, event.key);
    if (target === null) return;
    event.preventDefault();
    setTab(target);
    tabRefs.current[SESSION_TABS.findIndex((entry) => entry.key === target)]?.focus();
  };

  useEffect(() => {
    const key = `${sessionId}:${granularity}:${mode}`;
    const cached = cacheRef.current.get(key);
    setTimeline(cached ?? null);
    setTimelineError(null);
    setPinnedSeq(null);
    setTimelineLoading(cached === undefined);
    if (cached) return undefined;
    let cancelled = false;
    getContextTimeline(sessionId, granularity, mode)
      .then((next) => {
        cacheRef.current.set(key, next);
        if (!cancelled) setTimeline(next);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setTimelineError(cause instanceof Error ? cause.message : '上下文数据加载失败');
      })
      .finally(() => {
        if (!cancelled) setTimelineLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, granularity, mode, timelineReload]);

  useEffect(() => {
    setFleet(null);
    setFleetError(null);
  }, [sessionId]);

  useEffect(() => {
    if (tab !== 'fleet') return undefined;
    let cancelled = false;
    setFleetLoading(true);
    setFleetError(null);
    getSessionFleet(sessionId)
      .then((next) => {
        if (!cancelled) setFleet(next);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setFleetError(cause instanceof Error ? cause.message : 'Fleet 数据加载失败');
      })
      .finally(() => {
        if (!cancelled) setFleetLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sessionId, tab, fleetReload]);

  const markers = timeline ? attachEventMarkers(timeline.requests, timeline.events) : [];
  return (
    <section className="ctx-insights-page" aria-label="会话上下文洞察">
      <header className="ctx-insights-topbar">
        <div className="ctx-insights-topbar-inner">
          <button ref={backRef} type="button" className="ctx-insights-back" onClick={onClose}>
            <span aria-hidden="true">←</span> 返回工作区
          </button>
          <div className="ctx-insights-heading">
            <h1>会话上下文</h1>
            <span className="mono">{sessionId}</span>
          </div>
          <div className="ctx-insights-tabs" role="tablist" aria-label="会话洞察子页">
            {SESSION_TABS.map((entry, index) => {
              const selected = tab === entry.key;
              return (
                <button
                  key={entry.key}
                  ref={(el) => {
                    tabRefs.current[index] = el;
                  }}
                  id={`ctx-insights-tab-${entry.key}`}
                  type="button"
                  role="tab"
                  aria-selected={selected}
                  aria-controls={`ctx-insights-panel-${entry.key}`}
                  // roving tabindex：整条 tab 只占一个 Tab 停靠点，内部用方向键走。
                  tabIndex={selected ? 0 : -1}
                  className={selected ? 'is-on' : ''}
                  onClick={() => setTab(entry.key)}
                  onKeyDown={handleTabKeyDown}
                >
                  {entry.label}
                </button>
              );
            })}
          </div>
        </div>
      </header>

      <div className="ctx-insights-body">
        <div
          id="ctx-insights-panel-context"
          className="ctx-insights-panel"
          role="tabpanel"
          aria-labelledby="ctx-insights-tab-context"
          hidden={tab !== 'context'}
        >
          {timeline === null ? (
            timelineLoading ? (
              <InsightsSkeleton label="正在加载上下文数据" blocks={3} />
            ) : timelineError !== null ? (
              <InsightsError
                title="上下文数据加载失败"
                message={timelineError}
                onRetry={() => setTimelineReload((value) => value + 1)}
              />
            ) : (
              <div className="ctx-insights-empty">这个会话还没有上下文数据</div>
            )
          ) : (
            <>
              <ContextComposition usage={timeline.window} />
              <section className="ctx-insights-card">
                <header className="ctx-insights-card-head">
                  <div>
                    <h2>{granularity === 'turn' ? '逐轮趋势' : '逐请求趋势'}</h2>
                    <p className="ctx-insights-card-sub">
                      {granularity === 'turn'
                        ? mode === 'delta'
                          ? '增量 = 相邻轮 total 的差值；首行固定 0，耗时不做差分'
                          : '每轮最后一次请求的 total token；浅色段是其中缓存命中的部分'
                        : mode === 'delta'
                          ? '增量 = 相邻请求 total 的差值；首行固定 0，耗时不做差分'
                          : '每次请求的 total token；浅色段是其中缓存命中的部分'}
                    </p>
                  </div>
                  <div className="ctx-insights-trend-controls">
                    <div className="ctx-insights-seg" role="group" aria-label="趋势粒度">
                      <button
                        type="button"
                        className={granularity === 'step' ? 'is-on' : ''}
                        aria-pressed={granularity === 'step'}
                        onClick={() => setGranularity('step')}
                      >
                        按请求
                      </button>
                      <button
                        type="button"
                        className={granularity === 'turn' ? 'is-on' : ''}
                        aria-pressed={granularity === 'turn'}
                        onClick={() => setGranularity('turn')}
                      >
                        按轮
                      </button>
                    </div>
                    <div className="ctx-insights-seg" role="group" aria-label="趋势模式">
                      <button
                        type="button"
                        className={mode === 'total' ? 'is-on' : ''}
                        aria-pressed={mode === 'total'}
                        onClick={() => setMode('total')}
                      >
                        总量
                      </button>
                      <button
                        type="button"
                        className={mode === 'delta' ? 'is-on' : ''}
                        aria-pressed={mode === 'delta'}
                        onClick={() => setMode('delta')}
                      >
                        增量
                      </button>
                    </div>
                  </div>
                </header>
                <div className="ctx-insights-trend-legend">
                  <span><i className="is-total" />total token</span>
                  {mode === 'total' && <span><i className="is-cached" />其中缓存命中</span>}
                  {timeline.events.length > 0 && <span><i className="is-event" />上下文事件</span>}
                </div>
                <TrendChart
                  requests={timeline.requests}
                  markers={markers}
                  granularity={granularity}
                  mode={mode}
                  pinnedSeq={pinnedSeq}
                  onPin={setPinnedSeq}
                />
              </section>
              <ContextEvents events={timeline.events} />
            </>
          )}
        </div>

        <div
          id="ctx-insights-panel-fleet"
          className="ctx-insights-panel"
          role="tabpanel"
          aria-labelledby="ctx-insights-tab-fleet"
          hidden={tab !== 'fleet'}
        >
          {fleet === null ? (
            fleetLoading ? (
              <InsightsSkeleton label="正在加载 Fleet 数据" blocks={3} />
            ) : fleetError !== null ? (
              <InsightsError
                title="Fleet 数据加载失败"
                message={fleetError}
                onRetry={() => setFleetReload((value) => value + 1)}
              />
            ) : (
              <div className="ctx-insights-empty">这个会话还没有 Fleet 数据</div>
            )
          ) : (
            <>
              {fleetError !== null && (
                <InsightsError
                  title="Fleet 数据刷新失败"
                  message={fleetError}
                  onRetry={() => setFleetReload((value) => value + 1)}
                />
              )}
              <FleetView fleet={fleet} onOpenSession={onOpenSession} />
            </>
          )}
        </div>
      </div>
    </section>
  );
}
