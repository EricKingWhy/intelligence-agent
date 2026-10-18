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
 *  **#1307 子智能体折叠行（T5）**：只有 `product_state === 'executing'` 的卡渲染
 *  折叠按钮（Orca `AgentKanbanCard.tsx:288-305` 同构：真 `<button>` + `aria-expanded`）。
 *  **首次展开才拉** `GET /api/sessions/{id}/delegations`：看板打开时不为执行中卡预拉，
 *  避免 N×`/delegations` fan-out；C3 明令不进 sessions 投影。空结果缓存、
 *  失败态允许重试（失败不缓存），缓存放在卡片本地态里；`KanbanPanel` 关闭时整块板
 *  `return null` ⇒ 卡片卸载 ⇒ 缓存自然失效，正是"缓存到看板关闭"的语义，
 *  不需要额外失效逻辑。
 *  行内格式化全部复用 roster 的单一事实源（`STATUS_LABEL` / `tokenText` /
 *  `durationText`），本文件不写第二份映射。耗时基准用看板下发的 `now`（与相对时间同源）。
 *
 *  **本票不含**：最后一条消息（T6）、PR pill / worktree / repo 页脚（AC-16 明确
 *  断言页脚**只有**时间节点）。
 */

import { useRef, useState } from 'react';
import { AlertCircle, ChevronDown, ShieldQuestion } from 'lucide-react';
import type { SessionSummary } from '../types';
import { formatRelativeTime } from '../lib/format';
import { cardTitle } from '../lib/kanban';
import { getSessionDelegations, type DelegationEntry } from '../lib/api';
import { DELEGATION_STATUS_CLASS } from '../lib/delegationStatus';
import { STATUS_LABEL, durationText, tokenText } from './SubagentRoster';

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

/** 折叠行数据态：`idle` = 还没拉过（首次展开前）；`ready` 的**空数组也是结果**
 *  （「无子智能体」），所以它不能退回 `idle`——否则再展开会重拉。 */
type RosterState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; rows: DelegationEntry[] }
  | { status: 'error' };

export function KanbanCard({ session, now, draggable, onDragStart, unread, onOpen }: Props) {
  // 后端契约保证 `attention_kind` / `attention_summary` **恒成对**：`_attention` 成对返回，
  // 读盘校验 `(kind is None) != (summary is None)` 即判非法，派生失败兜底 `_empty` 也两者皆空
  // （`board_projection.py`）；列表按行独立取投影，故单边值到不了前端。染色 / 圆点 / 横幅
  // 三处因此共用一个布尔值，不写不可达的单边分支。
  const hasAttention = session.attention_kind !== null && session.attention_summary !== null;
  const empty = session.first_user_message === null;
  const title = cardTitle(session);
  const relative = formatRelativeTime(session.last_event_time, now);

  // #1307：只有执行中的卡有子智能体可看（其余三态的子智能体在票面语义里不存在）。
  const canExpand = session.product_state === 'executing';
  const [expanded, setExpanded] = useState(false);
  const [roster, setRoster] = useState<RosterState>({ status: 'idle' });
  /** 在途请求去重：`setState` 是异步的，连点两下时第二次仍会读到旧的 `idle`。
   *  用 ref 而不是靠状态判断——这是**请求去重**，不是渲染态。 */
  const fetching = useRef(false);

  const toggleSubagents = () => {
    const next = !expanded;
    setExpanded(next);
    if (!next) return;
    // 缓存：已拉过（含空结果）就只展开，不再请求。失败态允许重试。
    if (roster.status !== 'idle' && roster.status !== 'error') return;
    if (fetching.current) return;
    fetching.current = true;
    setRoster({ status: 'loading' });
    getSessionDelegations(session.session_id)
      .then((rows) => setRoster({ status: 'ready', rows: Array.isArray(rows) ? rows : [] }))
      .catch(() => setRoster({ status: 'error' }))
      .finally(() => {
        fetching.current = false;
      });
  };

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

      {canExpand && (
        // 卡片自身是 column flex + gap，这层只是把「按钮 + 结果」收成一组；组内
        // 间距与卡内一致（6px），不新增 CSS（本票只改两个组件文件）。
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          {/* 真 `<button>` + `aria-expanded`（Orca `AgentKanbanCard.tsx:288-305` 同构）。
              卡本身是 `role="button"`，所以点击/键盘 Enter/Space 必须 `stopPropagation`——
              否则展开子智能体会顺手把会话打开（嵌套交互事故）。
              其他键（如 Esc 关看板）放行给祖先处理。
              `.agent-trigger` 的 `.chev` 在 `.open` 时旋转 180°（既有 CSS，不新增）。 */}
          <button
            type="button"
            className={`agent-trigger${expanded ? ' open' : ''}`}
            aria-expanded={expanded}
            style={{ alignSelf: 'flex-start' }}
            onClick={(event) => {
              event.stopPropagation();
              toggleSubagents();
            }}
            onKeyDown={(event) => {
              // 只拦截按钮的激活键（Enter/Space）：卡本身是 `role="button"`，
              // 这两个键冒泡会触发祖先的键盘打开逻辑（嵌套交互事故）；
              // 其他键（如 Esc 关看板）放行给祖先处理。
              if (event.key === 'Enter' || event.key === ' ') {
                event.stopPropagation();
              }
            }}
          >
            <ChevronDown size={14} className="chev" aria-hidden="true" />
            <span className="roster-label">子智能体</span>
          </button>

          {expanded && roster.status === 'loading' && (
            <p className="kanban-card-hint">加载中…</p>
          )}
          {expanded && roster.status === 'error' && (
            <p className="kanban-card-hint">子智能体加载失败</p>
          )}
          {expanded && roster.status === 'ready' && roster.rows.length === 0 && (
            <p className="kanban-card-hint">无子智能体</p>
          )}
          {expanded &&
            roster.status === 'ready' &&
            roster.rows.map((entry) => {
              const duration = durationText(entry, now);
              return (
                <div
                  key={entry.child_session_id}
                  className="agent-row"
                  /* 复用 roster 行骨架，但这里是**静态展示**（卡内点击已归卡片自己）：
                     去掉指针语义与行尾的 22px 分栏箭头列。 */
                  style={{
                    cursor: 'default',
                    gridTemplateColumns: '16px minmax(0, 1fr) auto',
                  }}
                >
                  {/* 状态色后缀走 `DELEGATION_STATUS_CLASS` 单一事实源（`completed→success`）；
                      `.act-status` 是既有的实心圆徽章，这里不画 icon——卡内已有中文状态词，
                      再复制一份 icon 映射就是第三份。 */}
                  <span
                    className={`act-status act-status-${DELEGATION_STATUS_CLASS[entry.status]}`}
                    aria-hidden="true"
                  />
                  <div className="agent-copy">
                    <div className="agent-title">{entry.target || '?'}</div>
                    <div className="agent-sub">{entry.task || '—'}</div>
                  </div>
                  <div className="agent-stats">
                    <span className="status-word">{STATUS_LABEL[entry.status]}</span>
                    <span>{tokenText(entry.used_tokens, entry.usage_state)}</span>
                    {duration !== null && <span>{duration}</span>}
                  </div>
                </div>
              );
            })}
        </div>
      )}

      <footer className="kanban-card-foot">
        {/* 页脚**只有**时间（AC-16）：没有 PR pill、worktree、repo。 */}
        {relative !== '' && <time className="kanban-card-time">{relative}</time>}
      </footer>
    </article>
  );
}
