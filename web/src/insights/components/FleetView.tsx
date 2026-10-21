/** Agent 家族卡片（BUILD：dsh-context 的 Fleet 是网络图 + 任务板；本票后端只给
 *  当前 / 父 / 子三组卡片字段，故只做卡片列，不搬图——没有的数据不画）。
 *
 *  根会话 `ancestors` 为空 ⇒ 父列整列隐藏（票面）。每张卡只有 status / tokens /
 *  last_active_at 三个事实，不出现成本字段。 */

import type { ReactElement } from 'react';
import type { FleetAgentCard, FleetChildCard, FleetPayload } from '../../lib/api';
import { formatDateTime, formatRelativeTime } from '../../lib/format';
import { FLEET_STATUS_LABELS, formatCount } from '../lib/insightsModel';

function AgentCard({
  card,
  label,
  tokens,
  onOpen,
}: {
  card: FleetAgentCard | FleetChildCard;
  label: string;
  tokens: number;
  onOpen?: (sessionId: string) => void;
}): ReactElement {
  const lastActive = card.last_active_at;
  const body = (
    <>
      <div className="ctx-insights-fleet-card-head">
        <span className={`ctx-insights-fleet-status is-${card.status.replace(/[^a-z_]/gi, '') || 'unknown'}`}>
          {FLEET_STATUS_LABELS[card.status] ?? card.status}
        </span>
        <span className="ctx-insights-fleet-role">{label}</span>
      </div>
      <div className="ctx-insights-fleet-id mono">{card.session_id}</div>
      <div className="ctx-insights-fleet-tokens">
        <b>{formatCount(tokens)}</b>
        <span>字</span>
      </div>
      <div className="ctx-insights-fleet-active" title={lastActive ? formatDateTime(lastActive) ?? undefined : undefined}>
        {lastActive ? `最近活动 ${formatRelativeTime(lastActive)}` : '没有活动记录'}
      </div>
    </>
  );
  if (onOpen === undefined) {
    return <div className="ctx-insights-fleet-card">{body}</div>;
  }
  return (
    <button
      type="button"
      className="ctx-insights-fleet-card is-link"
      onClick={() => onOpen(card.session_id)}
      title="打开这个会话"
    >
      {body}
    </button>
  );
}

export function FleetView({
  fleet,
  onOpenSession,
}: {
  fleet: FleetPayload;
  onOpenSession: (sessionId: string) => void;
}): ReactElement {
  const hasAncestors = fleet.ancestors.length > 0;
  return (
    <div className={`ctx-insights-fleet${hasAncestors ? '' : ' no-ancestors'}`}>
      {hasAncestors && (
        <section className="ctx-insights-fleet-col" aria-label="父代理">
          <h3 className="ctx-insights-fleet-col-title">父代理</h3>
          {fleet.ancestors.map((card, index) => (
            <AgentCard
              key={card.session_id}
              card={card}
              label={index === fleet.ancestors.length - 1 ? '直接父级' : '祖先'}
              tokens={card.tokens}
              onOpen={onOpenSession}
            />
          ))}
        </section>
      )}
      <section className="ctx-insights-fleet-col" aria-label="当前代理">
        <h3 className="ctx-insights-fleet-col-title">当前代理</h3>
        <AgentCard card={fleet.self} label="本会话" tokens={fleet.self.tokens} />
      </section>
      <section className="ctx-insights-fleet-col" aria-label="子代理">
        <h3 className="ctx-insights-fleet-col-title">子代理</h3>
        {fleet.children.length === 0 ? (
          <div className="ctx-insights-empty">本会话还没有委派子代理</div>
        ) : (
          fleet.children.map((card) => (
            <AgentCard
              key={card.session_id}
              card={card}
              label="子代理"
              tokens={card.used_tokens}
              onOpen={onOpenSession}
            />
          ))
        )}
      </section>
    </div>
  );
}
