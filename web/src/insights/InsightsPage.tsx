/** 跨会话洞察页（#1420）：全屏浮层，从会话栏入口进入。（ADAPT：dsh-context
 *  `src/client/components/overviewPanel.tsx` @1bc481c，Apache-2.0；页面信息架构
 *  沿用，渲染与数据流是本仓针对 overview DTO 的新代码。）
 *
 *  页面外壳永远先渲染——API 失败只把内容区降级成错误 / 空态，绝不白屏（票面铁律）。
 *  所有数字来自 `GET /api/insights/overview`；热力图点日只做客户端过滤，不回源。
 *  只 token 口径，页面没有任何成本 / 美元字段。 */

import { useEffect, useMemo, useRef, useState, type ReactElement } from 'react';
import type { InsightsOverview, InsightsRange, InsightsSessionRow } from '../lib/api';
import { getInsightsOverview } from '../lib/api';
import {
  compositionSegments,
  compositionTotal,
  filterSessions,
  formatActiveMs,
  formatCompact,
  formatCount,
  formatRate,
  groupSessions,
  SESSION_SORTS,
  sortSessions,
  type SessionSort,
} from './lib/insightsModel';
import { useContextSizes } from './lib/useContextSizes';
import { DailyChart } from './components/DailyChart';
import { Donut } from './components/Donut';
import { Heatmap } from './components/Heatmap';
import { InsightsError, InsightsSkeleton } from './components/InsightsStatus';
import { SessionCard } from './components/SessionCard';
import './insights.css';

const RANGE_LABELS: Readonly<Record<InsightsRange, string>> = {
  '7d': '近 7 天',
  '30d': '近 30 天',
  all: '全部时间',
};

/** 空数据时的稳定引用：`data?.sessions ?? []` 每次渲染都会换数组，拖垮下游 memo。 */
const NO_SESSIONS: readonly InsightsSessionRow[] = [];

export interface InsightsPageProps {
  onClose: () => void;
  onOpenSession: (sessionId: string) => void;
}

export function InsightsPage({ onClose, onOpenSession }: InsightsPageProps): ReactElement {
  const [range, setRange] = useState<InsightsRange>('7d');
  const [data, setData] = useState<InsightsOverview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  const [query, setQuery] = useState('');
  const [day, setDay] = useState<string | null>(null);
  const [sort, setSort] = useState<SessionSort>('recent');
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  const backRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    backRef.current?.focus();
  }, []);

  // Esc 先清搜索：输入框有内容时只清空，不关整页；空查询才走既有关闭行为。
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      if (
        event.target instanceof HTMLInputElement &&
        event.target.type === 'search' &&
        query !== ''
      ) {
        event.preventDefault();
        setQuery('');
        return;
      }
      onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose, query]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    // 时间窗变了就先清旧数据：否则 30 天请求失败时，页面会拿 7 天的数字顶着
    // 「近 30 天」的标签继续展示——那是比空态更糟的错账。
    setData(null);
    getInsightsOverview(range)
      .then((next) => {
        if (!cancelled) setData(next);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setError(cause instanceof Error ? cause.message : '洞察数据加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [range, reloadKey]);

  const rows = data?.sessions ?? NO_SESSIONS;
  const sessionIds = useMemo(() => rows.map((row) => row.session_id), [rows]);
  const { sizes, loading: sizesLoading } = useContextSizes(sessionIds, sort === 'context');
  const filtered = useMemo(
    () => sortSessions(filterSessions(rows, { text: query, day }), sort, sizes),
    [rows, query, day, sort, sizes],
  );
  const groups = useMemo(() => groupSessions(filtered), [filtered]);
  const segments = data ? compositionSegments(data.composition) : [];
  const compositionSum = data ? compositionTotal(data.composition) : 0;
  const filtersActive = query.trim() !== '' || day !== null;

  return (
    <section className="ctx-insights-page" aria-label="上下文洞察">
      <header className="ctx-insights-topbar">
        <div className="ctx-insights-topbar-inner">
          <button
            ref={backRef}
            type="button"
            className="ctx-insights-back"
            onClick={onClose}
          >
            <span aria-hidden="true">←</span> 返回工作区
          </button>
          <div className="ctx-insights-heading">
            <h1>上下文洞察</h1>
            <span>只按 token 口径统计 · 不显示成本</span>
          </div>
          <div className="ctx-insights-range" role="group" aria-label="统计时间窗">
            {(['7d', '30d', 'all'] as const).map((key) => (
              <button
                key={key}
                type="button"
                className={range === key ? 'is-on' : ''}
                aria-pressed={range === key}
                onClick={() => setRange(key)}
              >
                {RANGE_LABELS[key]}
              </button>
            ))}
          </div>
        </div>
      </header>

      <div className="ctx-insights-body">
        {error !== null && (
          <InsightsError
            title="洞察数据加载失败"
            message={error}
            onRetry={() => setReloadKey((value) => value + 1)}
          />
        )}

        {data === null ? (
          loading ? (
            <InsightsSkeleton label="正在加载洞察数据" blocks={6} />
          ) : error === null ? (
            <div className="ctx-insights-empty">还没有可展示的洞察数据</div>
          ) : null
        ) : (
          <>
            <div className="ctx-insights-kpis">
              <Kpi label="会话" value={formatCount(data.kpi.sessions)} unit="个" />
              <Kpi label="计费 token" value={formatCount(data.kpi.billed_tokens)} unit="字" />
              <Kpi
                label="缓存命中"
                value={formatRate(data.kpi.cache_hit_rate)}
                unit={data.kpi.cache_hit_rate === null ? '无缓存明细' : ''}
              />
              <Kpi label="轮数" value={formatCount(data.kpi.turns)} unit="轮" />
              <Kpi label="工具调用" value={formatCount(data.kpi.tool_runs)} unit="次" />
              <Kpi label="活跃时长" value={formatActiveMs(data.kpi.active_ms)} unit="" />
            </div>

            <div className="ctx-insights-grid-2">
              <section className="ctx-insights-card">
                <header className="ctx-insights-card-head">
                  <div>
                    <h2>每日计费用量</h2>
                    <p className="ctx-insights-card-sub">{RANGE_LABELS[range]} · 按请求的 total token 折叠</p>
                  </div>
                </header>
                <DailyChart points={data.daily} />
              </section>

              <section className="ctx-insights-card">
                <header className="ctx-insights-card-head">
                  <div>
                    <h2>近 8 周活动</h2>
                    <p className="ctx-insights-card-sub">点有数据的一天，只看最近活动落在当天的会话</p>
                  </div>
                  {day !== null && (
                    <button type="button" className="ctx-insights-chip" onClick={() => setDay(null)}>
                      {day} ×
                    </button>
                  )}
                </header>
                <Heatmap points={data.heatmap} selected={day} onSelect={setDay} />
              </section>
            </div>

            <section className="ctx-insights-card">
              <header className="ctx-insights-card-head">
                <div>
                  <h2>构成快照</h2>
                  <p className="ctx-insights-card-sub">
                    覆盖 {data.coverage.snapshotted_sessions}/{data.coverage.total_sessions} 个会话；
                    没有快照的会话不计入，也不伪造构成
                  </p>
                </div>
              </header>
              {compositionSum > 0 ? (
                <div className="ctx-insights-composition">
                  <Donut
                    segments={segments}
                    centerTop={formatCompact(compositionSum)}
                    centerSub="字 · 快照合计"
                    hoverKey={hoverKey}
                    onHoverKey={setHoverKey}
                  />
                  <div className="ctx-insights-legend" onMouseLeave={() => setHoverKey(null)}>
                    {segments.map((segment) => (
                      <div
                        key={segment.key}
                        className={`ctx-insights-legend-row${hoverKey === segment.key ? ' is-on' : ''}`}
                        onMouseEnter={() => setHoverKey(segment.key)}
                      >
                        <span className="ctx-insights-legend-dot" style={{ background: segment.color }} />
                        <span className="ctx-insights-legend-label">{segment.label}</span>
                        <span className="ctx-insights-legend-value">{formatCount(segment.value)}</span>
                        <span className="ctx-insights-legend-pct">{segment.pct.toFixed(1)}%</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : (
                <div className="ctx-insights-empty">所选时间窗没有可用的构成快照</div>
              )}
            </section>

            <section className="ctx-insights-sessions" aria-label="会话列表">
              <header className="ctx-insights-sessions-head">
                <div>
                  <h2>会话</h2>
                  <p className="ctx-insights-card-sub">
                    {filtered.length === rows.length
                      ? `${rows.length} 个会话`
                      : `${filtered.length} / ${rows.length} 个会话`}
                    {sort === 'context' && sizesLoading ? ' · 正在读取窗口占用…' : ''}
                  </p>
                </div>
                <div className="ctx-insights-sessions-tools">
                  <input
                    type="search"
                    value={query}
                    placeholder="搜索会话 / 工作区"
                    aria-label="搜索会话"
                    onChange={(event) => setQuery(event.target.value)}
                  />
                  <div className="ctx-insights-sort" role="group" aria-label="会话排序">
                    {SESSION_SORTS.map((option) => (
                      <button
                        key={option.key}
                        type="button"
                        className={sort === option.key ? 'is-on' : ''}
                        aria-pressed={sort === option.key}
                        onClick={() => setSort(option.key)}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                </div>
              </header>

              {filtered.length === 0 ? (
                <div className="ctx-insights-empty">
                  <span>{filtersActive ? '没有匹配的会话' : '所选时间窗没有会话'}</span>
                  {filtersActive && (
                    <button
                      type="button"
                      className="ctx-insights-link"
                      onClick={() => { setQuery(''); setDay(null); }}
                    >
                      清除筛选
                    </button>
                  )}
                </div>
              ) : (
                groups.map((group) => (
                  <div key={group.workspace ?? '__none__'} className="ctx-insights-group">
                    <h3 className="ctx-insights-group-title">
                      <span>{group.workspace ?? '未分组'}</span>
                      <span className="ctx-insights-group-count">{group.rows.length}</span>
                    </h3>
                    <div className="ctx-insights-session-list">
                      {group.rows.map((row) => (
                        <SessionCard
                          key={row.session_id}
                          row={row}
                          contextSize={sort === 'context' ? sizes[row.session_id] : undefined}
                          onOpen={onOpenSession}
                        />
                      ))}
                    </div>
                  </div>
                ))
              )}
            </section>
          </>
        )}
      </div>
    </section>
  );
}

function Kpi({ label, value, unit }: { label: string; value: string; unit: string }): ReactElement {
  return (
    <div className="ctx-insights-kpi">
      <span className="ctx-insights-kpi-label">{label}</span>
      <span className="ctx-insights-kpi-value">
        {value}
        {unit !== '' && <small>{unit}</small>}
      </span>
    </div>
  );
}
