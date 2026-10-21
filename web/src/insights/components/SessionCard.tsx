/** 会话卡：首条用户消息（后端回落 session_id）、计费 token、轮数、最近活动。
 *  后端 overview 行**没有**逐会话构成字段——卡片不画假环形图（票面合同缺口，
 *  见交付报告），只展示行里真实存在的四个事实。 */

import type { ReactElement } from 'react';
import type { InsightsSessionRow } from '../../lib/api';
import { formatDateTime, formatRelativeTime } from '../../lib/format';
import { formatCount } from '../lib/insightsModel';

export interface SessionCardProps {
  row: InsightsSessionRow;
  /** 懒加载的窗口占用（仅「上下文大小」排序时取）；未知不显示。 */
  contextSize?: number | null;
  onOpen: (sessionId: string) => void;
}

export function SessionCard({ row, contextSize, onOpen }: SessionCardProps): ReactElement {
  const title = row.title.trim() || row.session_id;
  return (
    <button
      type="button"
      className="ctx-insights-session-card"
      onClick={() => onOpen(row.session_id)}
      aria-label={`打开会话 ${title}`}
      title={title === row.session_id ? row.session_id : `${title}\n${row.session_id}`}
    >
      <span className="ctx-insights-session-main">
        <span className="ctx-insights-session-title">{title}</span>
        <span className="ctx-insights-session-meta">
          <span className="mono">{row.session_id}</span>
          <span>{formatCount(row.turns)} 轮</span>
          {typeof contextSize === 'number' && (
            <span>窗口 {formatCount(contextSize)} 字</span>
          )}
          <span title={formatDateTime(row.last_active_at) ?? undefined}>
            {row.last_active_at ? formatRelativeTime(row.last_active_at) : '没有活动记录'}
          </span>
        </span>
      </span>
      <span className="ctx-insights-session-tokens">
        <b>{formatCount(row.billed_tokens)}</b>
        <span>字</span>
      </span>
    </button>
  );
}
