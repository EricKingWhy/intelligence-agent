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
 *  **#1306 未读（T4）**：未读卡标题加粗（Orca `AgentKanbanCard.tsx:247` 的
 *  `font-semibold`），点击卡片 = 写 ack + 打开会话（ack 写序见 `KanbanPanel`）。
 *  是否未读由看板算好下发（比较键是 `product_state_changed_at`，见 `lib/kanbanUnread.ts`），
 *  卡片只渲染结果。
 *
 *  **#1308 最后一条消息（Q10-A）**：正文两行——`You` + user 消息（单行 clamp）/
 *  `agent` + agent 文本（两行 clamp）。形状照抄 Orca `AgentKanbanCard.tsx:255-268`
 *  （`line-clamp-1` / `line-clamp-2`）；本仓无 Tailwind，clamp 落在
 *  `styles/kanban.css` 的 `.kanban-card-msg-*`（先例：`.kanban-card-attention-text`）。
 *  agent 名用固定 `agent` 前缀：本仓 `SessionSummary` 没有 agentType 字段，不为此
 *  新造一份契约；效果图（`kanban.html:150-151`）的 `你：` / `agent：` 也是固定前缀。
 *  **截断是写入侧的职责**（后端 128 字符），组件不再二次截断。
 *
 *  **本票不含**：子智能体折叠行（T5 / #1307）、PR pill / worktree / repo 页脚
 *  （AC-16 明确断言页脚**只有**时间节点）。
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
  /** 可拖条件由看板判（非空会话），卡片只渲染结果；"哪一列接受落点"由看板在
   *  drop 里判——卡片不持有落点语义。 */
  draggable: boolean;
  onDragStart: () => void;
  /** 未读（判据见 `lib/kanbanUnread.ts::unread`）。 */
  unread: boolean;
  /** 点卡片 / 键盘 Enter/Space：由看板先写 ack 再打开会话。 */
  onOpen: () => void;
}

export function KanbanCard({ session, now, draggable, onDragStart, unread, onOpen }: Props) {
  // 后端契约保证 `attention_kind` / `attention_summary` **恒成对**：`_attention` 成对返回，
  // 读盘校验 `(kind is None) != (summary is None)` 即判非法，派生失败兜底 `_empty` 也两者皆空
  // （`board_projection.py`）；列表按行独立取投影，故单边值到不了前端。染色 / 圆点 / 横幅
  // 三处因此共用一个布尔值，不写不可达的单边分支。
  const hasAttention = session.attention_kind !== null && session.attention_summary !== null;
  const empty = session.first_user_message === null;
  const title = cardTitle(session);
  const relative = formatRelativeTime(session.last_event_time, now);

  return (
    <article
      className={`kanban-card${hasAttention ? ' in-attention' : ''}${empty ? ' kanban-card-empty' : ''}`}
      data-session-id={session.session_id}
      data-unread={unread}
      /* 整卡可点（打开会话）。用 `role="button"` + 手写 Enter/Space，同
         `SubagentRoster.tsx:149-170` 的既有先例（不是真 `<button>`：卡内将来会有
         独立可交互元素，包在 button 里会产生嵌套交互）。 */
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(event) => {
        if (event.target !== event.currentTarget) return;
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          onOpen();
        }
      }}
      /* 空会话不可拖（§4.7）。**显式** `false`：属性缺失时 HTML 的 `draggable`
         默认是 `auto`（可拖），所以"不可拖"必须显式写出来才算钉住（AC-9②）。 */
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
        {hasAttention ? null : <span className="kanban-card-dot" aria-hidden="true" />}
        <span
          className={`kanban-card-title${unread ? ' is-unread' : ''}`}
          title={title}
        >
          {title}
        </span>
      </div>

      {session.last_user_message !== null || session.last_agent_message !== null ? (
        <div className="kanban-card-msgs">
          {session.last_user_message !== null && (
            <div className="kanban-card-msg-user kanban-card-msg-clamp-1">
              <span className="kanban-card-msg-prefix">You</span>{' '}
              {session.last_user_message}
            </div>
          )}
          {session.last_agent_message !== null && (
            <div className="kanban-card-msg-agent kanban-card-msg-clamp-2">
              <span className="kanban-card-msg-prefix">agent</span>{' '}
              {session.last_agent_message}
            </div>
          )}
        </div>
      ) : null}

      {empty && (
        // 空会话不是错误，是"还没开始"——次级说明而不是告警色（诚实但不吓人）。
        <p className="kanban-card-hint">空会话：请先在侧栏打开并发送消息</p>
      )}

      {hasAttention && (
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
