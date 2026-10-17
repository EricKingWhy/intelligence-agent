/** #1304 看板单卡：标题 / 相对时间 / 注意力横幅。
 *
 *  来源（抄优先，Orca @ `de1f6e3d8176beabdb189e71e6f3047bbe0edfad`，MIT）：
 *  - 横幅 `line-clamp-2` + 有横幅时不画状态点：
 *    `src/renderer/src/components/dashboard-popout/AgentKanbanCard.tsx:252`（`{card.askSummary ? null : <AgentStateDot .../>}`）
 *    与 `:280-285`（`<span className="line-clamp-2">{card.askSummary}</span>`）。
 *    不画点的理由照抄上游语义：横幅本身已经在说"这张卡在等你"，再画一个点是同一个
 *    信号说两遍，而点会被误读成"状态是 needs"。
 *  - 相对时间粗粒度：同文件 `:24-40` `formatStartedAgo`（just now / Nm / Nh / Nd）。
 *    本仓已有 `lib/format.ts::formatRelativeTime` 承担同一职责，**直接复用**（懒惰阶梯
 *    第 2 级：仓库已有），不抄第二份。
 *
 *  **本票不含**：未读样式（T4 / #1306）、子智能体折叠行（T5 / #1307）、最后一条
 *  消息（T6）、PR pill / worktree / repo 页脚（AC-16 明确断言页脚**只有**时间节点）。
 */

import { AlertCircle, ShieldQuestion } from 'lucide-react';
import type { SessionSummary } from '../types';
import { formatRelativeTime } from '../lib/format';
import { cardTitle } from '../lib/kanban';

interface Props {
  session: SessionSummary;
  /** 相对时间的基准（由看板统一下发一次，卡片不各自 `Date.now()`——
   *  否则同一次渲染里每张卡读到不同时刻，测试也就无法稳定断言）。 */
  now: number;
  /** 可拖条件由看板判（backlog 且非空会话），卡片只渲染结果。 */
  draggable: boolean;
  onDragStart: () => void;
}

export function KanbanCard({ session, now, draggable, onDragStart }: Props) {
  const attention = session.attention_kind !== null;
  const empty = session.first_user_message === null;
  const title = cardTitle(session);
  const relative = formatRelativeTime(session.last_event_time, now);

  return (
    <article
      className={`kanban-card${attention ? ' in-attention' : ''}${empty ? ' kanban-card-empty' : ''}`}
      data-session-id={session.session_id}
      /* 空会话不可拖（§4.7）。**显式** `false`：属性缺失时 HTML 的 `draggable`
         默认是 `auto`（可拖），所以"不可拖"必须显式写出来才算钉住。 */
      draggable={draggable}
      onDragStart={(event) => {
        // 兜底：`draggable={false}` 的元素正常不会起拖，但子元素/浏览器差异下仍可能
        // 冒泡上来一条 dragstart——不可拖的卡一律取消，避免"拖了没反应但状态变了"。
        if (!draggable) {
          event.preventDefault();
          return;
        }
        event.dataTransfer.effectAllowed = 'move';
        onDragStart();
      }}
    >
      <div className="kanban-card-title-row">
        {/* 有横幅时不画点（Orca `:252` 同构）。 */}
        {attention ? null : <span className="kanban-card-dot" aria-hidden="true" />}
        <span className="kanban-card-title" title={title}>
          {title}
        </span>
      </div>

      {empty && (
        // 空会话不是错误，是"还没开始"——次级说明而不是告警色（诚实但不吓人）。
        <p className="kanban-card-hint">空会话：请先在侧栏打开并发送消息</p>
      )}

      {session.attention_summary !== null && (
        <div className="kanban-card-attention" data-attention-kind={session.attention_kind ?? ''}>
          {session.attention_kind === 'approval' ? (
            <ShieldQuestion size={12} aria-hidden="true" />
          ) : (
            <AlertCircle size={12} aria-hidden="true" />
          )}
          <span className="kanban-card-attention-text">{session.attention_summary}</span>
        </div>
      )}

      <footer className="kanban-card-foot">
        {/* 页脚**只有**时间（AC-16）：没有 PR pill、worktree、repo。 */}
        {relative !== '' && <time className="kanban-card-time">{relative}</time>}
      </footer>
    </article>
  );
}
