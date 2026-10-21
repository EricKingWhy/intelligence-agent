/** 构成环（ADAPT：dsh-context `src/client/components/donut.tsx` @1bc481c，
 *  Apache-2.0；几何与间隙规则沿用，ViewKit / Tailwind 抽象换成本仓 token 与
 *  `.ctx-insights-*` 类）。
 *
 *  环只画**有值**的段；全零时画一条中性轨道，绝不画「100% 的什么都没有」。
 *  相邻段之间让出半个间隙（露出卡片底色），单个段保持整环。 */

import type { CSSProperties, ReactElement, ReactNode } from 'react';

/** 相邻段之间的发丝间隙（dasharray 单位：1 单位 ≈ 环周长的 1%）。 */
const SEG_GAP = 0.5;

export interface DonutSegment {
  key: string;
  label: string;
  color: string;
  value: number;
}

export interface DonutProps {
  segments: readonly DonutSegment[];
  centerTop: ReactNode;
  centerSub?: ReactNode;
  size?: number;
  hoverKey?: string | null;
  onHoverKey?: (key: string | null) => void;
}

export function Donut({
  segments,
  centerTop,
  centerSub,
  size = 132,
  hoverKey = null,
  onHoverKey,
}: DonutProps): ReactElement {
  let total = 0;
  for (const segment of segments) {
    if (Number.isFinite(segment.value) && segment.value > 0) total += segment.value;
  }
  const arcs: Array<{ key: string; color: string; len: number; offset: number }> = [];
  let acc = 0;
  if (total > 0) {
    for (const segment of segments) {
      const value = Number.isFinite(segment.value) && segment.value > 0 ? segment.value : 0;
      if (value === 0) continue;
      const pct = (value / total) * 100;
      // +25 把第一段的起点钉在 12 点方向。
      arcs.push({ key: segment.key, color: segment.color, len: pct, offset: 100 - acc + 25 });
      acc += pct;
    }
  }
  if (arcs.length > 1) {
    for (const arc of arcs) {
      const cut = Math.min(SEG_GAP / 2, arc.len / 4);
      arc.len -= cut * 2;
      arc.offset -= cut;
    }
  }
  const hovering = hoverKey !== null && arcs.some((arc) => arc.key === hoverKey);
  return (
    <div
      className={`ctx-insights-donut${hovering ? ' is-dimmed' : ''}`}
      style={{ width: size, height: size }}
      onMouseLeave={() => onHoverKey?.(null)}
    >
      <svg viewBox="0 0 42 42" width={size} height={size} aria-hidden="true">
        {arcs.length === 0 ? (
          <circle className="ctx-insights-donut-track" cx="21" cy="21" r="15.9155" />
        ) : (
          arcs.map((arc, index) => (
            <circle
              key={arc.key}
              className={`ctx-insights-donut-seg${hoverKey === arc.key ? ' is-on' : ''}`}
              cx="21"
              cy="21"
              r="15.9155"
              strokeDasharray={`${arc.len} ${100 - arc.len}`}
              strokeDashoffset={arc.offset}
              style={{ '--ctx-i': index, stroke: arc.color } as CSSProperties}
              onMouseEnter={() => onHoverKey?.(arc.key)}
            />
          ))
        )}
      </svg>
      <div className="ctx-insights-donut-center">
        <b style={{ fontSize: Math.max(12, Math.round(size * 0.125)) }}>{centerTop}</b>
        {centerSub !== undefined && (
          <span style={{ fontSize: Math.max(11, Math.round(size * 0.09)) }}>{centerSub}</span>
        )}
      </div>
    </div>
  );
}
