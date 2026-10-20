/** 逐请求 token 趋势（ADAPT：dsh-context `trendChart.tsx` @1bc481c，Apache-2.0；
 *  保留「一请求一柱 / hover brief / 点击 pin / 事件标记」的交互骨架，去掉 dsh
 *  的七类构成 DNA、turn 条带与自适应窗口——本票只有 total / delta 两个模式）。
 *
 *  数据是后端 `context-timeline` 已按 `granularity` / `mode` 处理好的序列；
 *  本组件不再做折叠或差分（避免与后端两套口径）。
 */

import { useLayoutEffect, useRef, useState, type ReactElement } from 'react';
import type { ContextEventPoint, ContextRequestPoint } from '../../lib/api';
import { formatDateTime } from '../../lib/format';
import {
  CONTEXT_EVENT_LABELS,
  formatCompact,
  requestFacts,
  trendScale,
} from '../lib/insightsModel';

const HEIGHT = 196;
const PAD_LEFT = 48;
const PAD_RIGHT = 10;
const PAD_TOP = 24;
const PAD_BOTTOM = 28;

export interface TrendChartProps {
  requests: readonly ContextRequestPoint[];
  markers: ReadonlyArray<ContextEventPoint | null>;
  mode: 'total' | 'delta';
  pinnedSeq: number | null;
  onPin: (seq: number | null) => void;
}

export function TrendChart({
  requests,
  markers,
  mode,
  pinnedSeq,
  onPin,
}: TrendChartProps): ReactElement {
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const { maxUp, maxDown } = trendScale(requests, mode);
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollLeft = node.scrollWidth;
  }, [requests]);
  if (requests.length === 0) {
    return <div className="ctx-insights-empty">还没有模型请求，趋势图暂无数据</div>;
  }
  if (maxUp <= 0 && maxDown <= 0) {
    return <div className="ctx-insights-empty">请求已记录，但用量字段为空</div>;
  }
  const width = Math.max(420, requests.length * 12 + PAD_LEFT + PAD_RIGHT);
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const step = (width - PAD_LEFT - PAD_RIGHT) / requests.length;
  const barWidth = Math.max(2, Math.min(8, step * 0.58));
  const totalSpan = maxUp + maxDown;
  const baseline = mode === 'delta'
    ? PAD_TOP + (totalSpan > 0 ? (maxUp / totalSpan) * plotHeight : plotHeight)
    : PAD_TOP + plotHeight;
  const labelEvery = Math.max(1, Math.ceil(requests.length / 8));
  const active = pinnedSeq !== null
    ? requests.find((request) => request.seq === pinnedSeq) ?? null
    : hoverIndex !== null ? requests[hoverIndex] ?? null : null;
  const moveHover = (delta: number) => {
    const current = hoverIndex ?? requests.length - 1;
    const next = Math.max(0, Math.min(requests.length - 1, current + delta));
    setHoverIndex(next);
  };
  return (
    <div className="ctx-insights-trend">
      <div
        className="ctx-insights-chart-scroll ctx-insights-trend-scroll"
        ref={scrollRef}
        tabIndex={0}
        role="group"
        aria-label="逐请求 token 趋势；左右方向键移动，Enter 固定明细"
        onKeyDown={(event) => {
          if (event.key === 'ArrowLeft') {
            event.preventDefault();
            moveHover(-1);
          } else if (event.key === 'ArrowRight') {
            event.preventDefault();
            moveHover(1);
          } else if (event.key === 'Enter' && hoverIndex !== null) {
            event.preventDefault();
            const request = requests[hoverIndex];
            if (request) onPin(pinnedSeq === request.seq ? null : request.seq);
          }
        }}
      >
        <svg
          className="ctx-insights-trend-svg"
          width={width}
          height={HEIGHT}
          viewBox={`0 0 ${width} ${HEIGHT}`}
          aria-hidden="true"
        >
          {mode === 'total' && [maxUp, maxUp / 2, 0].map((value) => {
            const y = PAD_TOP + plotHeight - (value / maxUp) * plotHeight;
            return (
              <g key={value}>
                <line className="ctx-insights-grid" x1={PAD_LEFT} x2={width - PAD_RIGHT} y1={y} y2={y} />
                <text className="ctx-insights-axis" x={PAD_LEFT - 8} y={y + 3} textAnchor="end">
                  {formatCompact(value)}
                </text>
              </g>
            );
          })}
          {mode === 'delta' && (
            <>
              <line className="ctx-insights-grid" x1={PAD_LEFT} x2={width - PAD_RIGHT} y1={PAD_TOP} y2={PAD_TOP} />
              <line className="ctx-insights-grid" x1={PAD_LEFT} x2={width - PAD_RIGHT} y1={baseline} y2={baseline} />
              <text className="ctx-insights-axis" x={PAD_LEFT - 8} y={PAD_TOP + 3} textAnchor="end">
                {`+${formatCompact(maxUp)}`}
              </text>
              <text className="ctx-insights-axis" x={PAD_LEFT - 8} y={baseline + 3} textAnchor="end">
                0
              </text>
              {maxDown > 0 && (
                <text className="ctx-insights-axis" x={PAD_LEFT - 8} y={PAD_TOP + plotHeight + 3} textAnchor="end">
                  {`-${formatCompact(maxDown)}`}
                </text>
              )}
            </>
          )}
          {requests.map((request, index) => {
            const value = request.total_tokens;
            const positive = value >= 0;
            const height = mode === 'delta'
              ? (positive
                ? (maxUp > 0 ? (value / maxUp) * plotHeight : 0)
                : (maxDown > 0 ? (-value / maxDown) * plotHeight : 0))
              : (value / maxUp) * plotHeight;
            const x = PAD_LEFT + index * step + (step - barWidth) / 2;
            const y = mode === 'delta' && !positive ? baseline : baseline - height;
            const cachedHeight = mode === 'total' && maxUp > 0
              ? (Math.min(request.cached_tokens, Math.max(value, 0)) / maxUp) * plotHeight
              : 0;
            const marker = markers[index] ?? null;
            const pinned = pinnedSeq === request.seq;
            return (
              <g key={request.seq}>
                {marker !== null && (
                  <rect
                    className="ctx-insights-trend-marker"
                    x={x}
                    y={PAD_TOP - 8}
                    width={barWidth}
                    height={4}
                    rx={1}
                  >
                    <title>{`#${marker.seq} ${CONTEXT_EVENT_LABELS[marker.type] ?? marker.type}`}</title>
                  </rect>
                )}
                <rect
                  className={`ctx-insights-trend-bar${pinned ? ' is-pinned' : ''}${mode === 'delta' && !positive ? ' is-negative' : ''}`}
                  x={x}
                  y={y}
                  width={barWidth}
                  height={Math.max(height, 0.5)}
                  rx={1.5}
                />
                {cachedHeight > 0 && (
                  <rect
                    className="ctx-insights-trend-cached"
                    x={x}
                    y={baseline - cachedHeight}
                    width={barWidth}
                    height={cachedHeight}
                    rx={1.5}
                  />
                )}
                <rect
                  className="ctx-insights-trend-hit"
                  x={PAD_LEFT + index * step}
                  y={PAD_TOP}
                  width={step}
                  height={plotHeight}
                  onMouseEnter={() => setHoverIndex(index)}
                  onMouseLeave={() => setHoverIndex(null)}
                  onClick={() => onPin(pinned ? null : request.seq)}
                />
                {(index % labelEvery === 0 || index === requests.length - 1) && (
                  <text
                    className="ctx-insights-axis"
                    x={x + barWidth / 2}
                    y={HEIGHT - 8}
                    textAnchor="middle"
                  >
                    {`#${request.seq}`}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
      </div>
      <div className="ctx-insights-trend-brief" aria-live="polite">
        {active === null ? (
          <span className="ctx-insights-trend-hint">悬停查看请求明细；点击柱固定，再点取消</span>
        ) : (
          <>
            <span className="ctx-insights-trend-brief-head">
              #{active.seq} · {formatDateTime(active.time) ?? active.time}
              {pinnedSeq === active.seq && <b>已固定</b>}
            </span>
            {requestFacts(active).map((fact) => (
              <span key={fact.label} className="ctx-insights-trend-fact">
                <span>{fact.label}</span>
                <b>{fact.value}</b>
              </span>
            ))}
          </>
        )}
      </div>
    </div>
  );
}
