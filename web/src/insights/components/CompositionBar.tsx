/** 当前窗口构成条（ADAPT：dsh-context `stackedBar.tsx` @1bc481c，Apache-2.0；
 *  只保留本票能对账的部分——分段 + 图例联动 + 70/85% 比例阈值线。
 *
 *  **不画 headroom / reserved output**：Owlet 没有这两个字段（票面裁决）。
 *  阈值线是窗口比例，不是 token 预留。 */

import type { ReactElement } from 'react';
import type { CompositionSegment } from '../lib/insightsModel';
import { thresholdMarks } from '../lib/insightsModel';

export interface CompositionBarProps {
  segments: readonly CompositionSegment[];
  /** 已用 token（分段之和；usage_only 时是下界）。 */
  used: number;
  /** 生效窗口（token）；≤0 时不画阈值线。 */
  windowTokens: number;
  thresholds: { auto_compact: number; hard_guard: number };
  hoverKey: string | null;
  onHoverKey: (key: string | null) => void;
}

export function CompositionBar({
  segments,
  used,
  windowTokens,
  thresholds,
  hoverKey,
  onHoverKey,
}: CompositionBarProps): ReactElement {
  const painted = segments.filter((segment) => segment.value > 0);
  const marks = windowTokens > 0 ? thresholdMarks(thresholds) : [];
  return (
    <div className="ctx-insights-compbar">
      <div
        className="ctx-insights-compbar-track"
        role="img"
        aria-label={painted.map((segment) => `${segment.label} ${segment.value.toLocaleString()} 字`).join('，')}
      >
        {painted.map((segment) => (
          <span
            key={segment.key}
            className={`ctx-insights-compbar-seg${hoverKey === segment.key ? ' is-on' : ''}`}
            style={{ flexGrow: segment.value, background: segment.color }}
            onMouseEnter={() => onHoverKey(segment.key)}
            onMouseLeave={() => onHoverKey(null)}
          />
        ))}
        {marks.map((mark) => (
          <span
            key={mark.label}
            className="ctx-insights-compbar-mark"
            style={{ left: `${mark.ratio * 100}%` }}
            title={`${mark.label}（窗口比例，非 token 预留）`}
          />
        ))}
      </div>
      <div className="ctx-insights-compbar-scale">
        <span>0</span>
        <span>{used.toLocaleString()} / {windowTokens.toLocaleString()} 字</span>
        <span>{windowTokens > 0 ? `${((used / windowTokens) * 100).toFixed(1)}%` : '—'}</span>
      </div>
    </div>
  );
}
