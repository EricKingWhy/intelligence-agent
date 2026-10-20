/** 每日计费用量（BUILD：dsh-context 的 overviewUsage 是双指标图，这里只保留本票
 *  能对账的 token 口径——后端 `daily` 的按日折叠值）。
 *
 *  一天一根柱；零是「那天没有请求」的真 0，不是估算。点数多时横向滚动，
 *  最右端是最新一天（打开即滚到最新）。 */

import { useLayoutEffect, useRef, type ReactElement } from 'react';
import type { InsightsDayPoint } from '../../lib/api';
import { formatCompact } from '../lib/insightsModel';

const HEIGHT = 168;
const PAD_LEFT = 44;
const PAD_RIGHT = 8;
const PAD_TOP = 14;
const PAD_BOTTOM = 24;

export interface DailyChartProps {
  points: readonly InsightsDayPoint[];
}

export function DailyChart({ points }: DailyChartProps): ReactElement {
  const scrollRef = useRef<HTMLDivElement>(null);
  let max = 0;
  let sum = 0;
  for (const point of points) {
    if (point.tokens > max) max = point.tokens;
    sum += point.tokens;
  }
  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (node) node.scrollLeft = node.scrollWidth;
  }, [points]);
  if (points.length === 0) {
    return <div className="ctx-insights-empty">所选时间窗没有每日用量</div>;
  }
  if (max <= 0) {
    return <div className="ctx-insights-empty">所选时间窗没有计费用量</div>;
  }
  const width = Math.max(360, points.length * 16 + PAD_LEFT + PAD_RIGHT);
  const plotHeight = HEIGHT - PAD_TOP - PAD_BOTTOM;
  const step = (width - PAD_LEFT - PAD_RIGHT) / points.length;
  const barWidth = Math.max(2, Math.min(10, step * 0.62));
  const labelEvery = Math.max(1, Math.ceil(points.length / 8));
  const gridValues = [max, max / 2, 0];
  return (
    <div className="ctx-insights-chart-scroll" ref={scrollRef}>
      <svg
        className="ctx-insights-daily"
        width={width}
        height={HEIGHT}
        viewBox={`0 0 ${width} ${HEIGHT}`}
        role="img"
        aria-label={`每日计费用量：共 ${sum.toLocaleString()} 字，单日最高 ${max.toLocaleString()} 字`}
      >
        {gridValues.map((value) => {
          const y = PAD_TOP + plotHeight - (value / max) * plotHeight;
          return (
            <g key={value}>
              <line className="ctx-insights-grid" x1={PAD_LEFT} x2={width - PAD_RIGHT} y1={y} y2={y} />
              <text className="ctx-insights-axis" x={PAD_LEFT - 8} y={y + 3} textAnchor="end">
                {formatCompact(value)}
              </text>
            </g>
          );
        })}
        {points.map((point, index) => {
          const height = (point.tokens / max) * plotHeight;
          const x = PAD_LEFT + index * step + (step - barWidth) / 2;
          const y = PAD_TOP + plotHeight - height;
          const label = `${point.date} · ${point.tokens.toLocaleString()} 字`;
          return (
            <g key={point.date}>
              <rect
                className="ctx-insights-daily-bar"
                x={x}
                y={y}
                width={barWidth}
                height={Math.max(point.tokens > 0 ? 1 : 0, height)}
                rx={2}
              >
                <title>{label}</title>
              </rect>
              {(index % labelEvery === 0 || index === points.length - 1) && (
                <text
                  className="ctx-insights-axis"
                  x={x + barWidth / 2}
                  y={HEIGHT - 8}
                  textAnchor="middle"
                >
                  {point.date.slice(5)}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
