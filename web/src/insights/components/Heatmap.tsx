/** 8 周活动热力图（ADAPT：dsh-context `src/client/components/heatmap.tsx` @1bc481c，
 *  Apache-2.0；GitHub 式周列 / 四档深度 / 点日过滤沿用，日期窗口与类名换成本仓口径）。
 *
 *  数据是后端 `heatmap` 的 56 天（近 8 周）计费 token；无数据日 = 真 0。
 *  点有数据的一天 → 会话卡按该天过滤；再点同一天 → 取消过滤。 */

import type { ReactElement } from 'react';
import type { InsightsDayPoint } from '../../lib/api';
import { formatCount, heatLevel, heatmapColumns, heatmapMax, heatmapToday } from '../lib/insightsModel';

const WEEKDAY_LABELS: Readonly<Record<number, string>> = { 1: '一', 3: '三', 5: '五' };

export interface HeatmapProps {
  points: readonly InsightsDayPoint[];
  selected: string | null;
  onSelect: (day: string | null) => void;
}

export function Heatmap({ points, selected, onSelect }: HeatmapProps): ReactElement {
  const today = heatmapToday(points);
  const columns = heatmapColumns(points, today, 8);
  const max = heatmapMax(points);
  if (columns === null) {
    return <div className="ctx-insights-empty">热力图数据不可用</div>;
  }
  if (max === 0) {
    return <div className="ctx-insights-empty">近 8 周没有计费用量</div>;
  }
  return (
    <div className="ctx-insights-heat-wrap">
      <div className="ctx-insights-heat" role="group" aria-label="近 8 周每日计费用量">
        <div className="ctx-insights-heat-wds" aria-hidden="true">
          {[0, 1, 2, 3, 4, 5, 6].map((row) => (
            <span key={row} className="ctx-insights-heat-wd">{WEEKDAY_LABELS[row] ?? ''}</span>
          ))}
        </div>
        <div className="ctx-insights-heat-cols">
          {columns.map((column) => (
            <div key={column.cells[0].key} className="ctx-insights-heat-col">
              <span className="ctx-insights-heat-mon" aria-hidden="true">
                {column.month !== null ? `${column.month}月` : ''}
              </span>
              {column.cells.map((cell) => {
                if (cell.future) {
                  return <span key={cell.key} className="ctx-insights-heat-cell is-future" aria-hidden="true" />;
                }
                const level = heatLevel(cell.tokens, max);
                if (cell.tokens <= 0) {
                  return (
                    <span
                      key={cell.key}
                      className="ctx-insights-heat-cell is-empty"
                      role="img"
                      title={`${cell.key} · 无用量`}
                      aria-label={`${cell.key} 无用量`}
                    />
                  );
                }
                const picked = selected === cell.key;
                const label = `${cell.key} · ${formatCount(cell.tokens)} 字`;
                return (
                  <button
                    key={cell.key}
                    type="button"
                    className={`ctx-insights-heat-cell is-level-${level}${picked ? ' is-picked' : ''}`}
                    title={label}
                    aria-label={label}
                    aria-pressed={picked}
                    onClick={() => onSelect(picked ? null : cell.key)}
                  />
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="ctx-insights-heat-legend" aria-hidden="true">
        <span>少</span>
        {[0, 1, 2, 3, 4].map((level) => (
          <span key={level} className={`ctx-insights-heat-cell is-level-${level}`} />
        ))}
        <span>多</span>
      </div>
    </div>
  );
}
