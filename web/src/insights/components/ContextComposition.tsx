/** 当前窗口构成（BUILD on existing `ContextUsage` contract）。六桶 + 工具两个子桶；
 *  `usage_only` / `no_data` 如实降级，不把「没分类」画成「没有占用」。
 *
 *  与既有 `ContextUsagePanel` 的差异：这里只读展示，不带压缩按钮 / 窗口挡位编辑
 *  （那是会话内操作面板的职责，本模块不改 RUN INSPECTOR）。 */

import { useState, type ReactElement } from 'react';
import type { ContextUsage } from '../../lib/api';
import {
  COMPOSITION_PARTS,
  compositionSegments,
  formatCount,
  formatRate,
} from '../lib/insightsModel';
import { CompositionBar } from './CompositionBar';

export function ContextComposition({ usage }: { usage: ContextUsage }): ReactElement {
  const [hoverKey, setHoverKey] = useState<string | null>(null);
  if (usage.state === 'no_data') {
    return (
      <section className="ctx-insights-card">
        <header className="ctx-insights-card-head">
          <h2>当前窗口构成</h2>
        </header>
        <div className="ctx-insights-empty">本会话还没有可用的上下文数据</div>
      </section>
    );
  }
  const segments = usage.state === 'ok'
    ? compositionSegments({
      messages: usage.breakdown.messages,
      system_prompt: usage.breakdown.system_prompt,
      skills: usage.breakdown.skills,
      other: usage.breakdown.other,
      tools_system: usage.breakdown.tools.system,
      tools_mcp: usage.breakdown.tools.mcp,
    })
    : [{
      key: 'other' as const,
      label: '未分类',
      color: 'var(--text-tertiary)',
      value: usage.used_tokens,
      pct: 100,
    }];
  const usageOnly = usage.state === 'usage_only';
  const cache = usage.cache;
  const cacheText = cache.state === 'not_collected'
    ? '缓存未采集'
    : cache.avg_hit_rate === null
      ? '缓存命中 —'
      : `缓存命中 ${formatRate(cache.avg_hit_rate)}（${cache.reported_calls}/${cache.total_calls} 次调用有明细）`;
  return (
    <section className="ctx-insights-card">
      <header className="ctx-insights-card-head">
        <div>
          <h2>当前窗口构成</h2>
          <p className="ctx-insights-card-sub">
            {usageOnly
              ? `仅用量口径：${usage.used_tokens.toLocaleString()} 字是窗口占用下界，分类未上报`
              : `六桶之和 = 快照总量；窗口 ${usage.window_tokens.toLocaleString()} 字`}
            {usage.estimated ? ' · 估算值' : ''}
          </p>
        </div>
        <span className={`ctx-insights-badge${usageOnly ? ' is-muted' : ''}`}>
          {usageOnly ? '仅用量' : '快照'}
        </span>
      </header>
      <CompositionBar
        segments={segments}
        used={usage.used_tokens}
        windowTokens={usage.window_tokens}
        thresholds={usage.thresholds}
        hoverKey={hoverKey}
        onHoverKey={setHoverKey}
      />
      <div className="ctx-insights-legend" onMouseLeave={() => setHoverKey(null)}>
        {segments.map((segment) => {
          const part = COMPOSITION_PARTS.find((candidate) => candidate.key === segment.key);
          return (
            <div
              key={segment.key}
              className={`ctx-insights-legend-row${hoverKey === segment.key ? ' is-on' : ''}`}
              onMouseEnter={() => setHoverKey(segment.key)}
            >
              <span className="ctx-insights-legend-dot" style={{ background: part?.color ?? segment.color }} />
              <span className="ctx-insights-legend-label">{segment.label}</span>
              <span className="ctx-insights-legend-value">{formatCount(segment.value)}</span>
              <span className="ctx-insights-legend-pct">
                {usageOnly ? '未分类' : `${segment.pct.toFixed(1)}%`}
              </span>
            </div>
          );
        })}
      </div>
      <div className="ctx-insights-card-foot">
        <span>{cacheText}</span>
        <span>阈值 70% / 85% 是窗口比例，不是 token 预留</span>
      </div>
    </section>
  );
}
