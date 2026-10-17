/** #1304 看板列 / 分组 / 排序 / 搜索的**纯函数层**（无 DOM、无 React）。
 *
 *  分层理由（照 `lib/capabilities.ts` 的既有分工）：板的结构决策全在这里，组件只做
 *  渲染接线。这样 vitest 能在没有 jsdom 的情况下把语义钉死。
 *
 *  来源（抄优先，Orca @ `de1f6e3d8176beabdb189e71e6f3047bbe0edfad`，MIT）：
 *  - 列顺序单一来源：`src/shared/dashboard-snapshot.ts:16-22` `DASHBOARD_BUCKET_ORDER`
 *    （注释原文 "Column order shared by producer and pop-out so they never drift"）。
 *  - 一卡一列 + 列内排序：`src/renderer/src/components/dashboard-popout/AgentKanbanBoard.tsx:59-66`
 *    （`grouped[card.bucket].push(card)` 后 `sort((a, b) => b.stateChangedAt - a.stateChangedAt)`）。
 *  - 搜索过滤：`.../agent-board-filtering.ts:27-52`（`toLocaleLowerCase()` + `includes`）。
 *
 *  **适配点**（票面「实现原则」允许改的只有这些）：
 *  - 桶换成后端 `PRODUCT_STATES` 的四态投影（`""` = backlog，不是 Orca 的四桶）；
 *  - 数据源换成 `GET /api/sessions` 的行（不是 Orca 的 store）；
 *  - 注意力**不做成桶**（C2）：它是覆盖层，见 `styles/kanban.css` 的 `.in-attention`。
 */

import type { SessionSummary } from '../types';

/** 列顺序的**唯一来源**——`KanbanPanel` 与搜索/计数都只从这里取，禁止第二处各写一份
 *  （Orca `DASHBOARD_BUCKET_ORDER` 的同一条纪律）。
 *
 *  值是后端 `product_state` 的**原样字面量**：`""` 就是 backlog。不做 `'' -> 'backlog'`
 *  的映射层——多一层映射就多一处能与后端契约漂移的地方，而 `data-testid` 也直接是
 *  `kanban-col-<state>`（票面 AC-1 的判据）。
 *
 *  `deliverable` **刻意不在表里**：C5 判定该列 v1 不渲染（criteria 链未通），但卡片
 *  也**不许静默丢**——计数由 `deliverableCount()` 单独报给看板顶部（不变量 #21）。 */
export const KANBAN_COLUMNS = ['', 'executing', 'pending_verification', 'accepted'] as const;

export type KanbanColumn = (typeof KANBAN_COLUMNS)[number];

/** 列标题（中文为主，与 `TaskReviewPanel` 的 `PRODUCT_LABEL` 同词）。 */
export const COLUMN_LABELS: Record<KanbanColumn, string> = {
  '': '待办',
  executing: '执行中',
  pending_verification: '待验证',
  accepted: '已接受',
};

/** v1 不渲染的列（C5）。单独导出是为了让"不渲染"这件事在代码里有名字，而不是
 *  靠 `KANBAN_COLUMNS` 里"少了一项"这种需要数数才看得出来。 */
export const UNRENDERED_STATE = 'deliverable';

/** 列容器的 `data-testid` 后缀（AC-1 的判据锚点，**与 `KANBAN_COLUMNS` 同一处维护**）。
 *
 *  **为什么 backlog 不直接用空串**：票面 AC-1 写的是 `data-testid="kanban-col-<state>"`，
 *  照字面读 backlog（`product_state === ""`）会得到 `kanban-col-`；而 Playwright 的
 *  `getByTestId` 默认是**子串**匹配（`exact: false`），`kanban-col-` 会同时命中另外三列
 *  ——AC-1「各恰好 1 张卡」当场失效。所以这里给每列一个稳定 slug，`backlog` 就是
 *  `''` 这一列的名字。**原始后端值不丢**：列容器同时带 `data-column="<product_state>"`，
 *  投影与契约的对应关系在 DOM 上依然可查（`data-column=""` = backlog）。 */
export const COLUMN_TESTIDS: Record<KanbanColumn, string> = {
  '': 'backlog',
  executing: 'executing',
  pending_verification: 'pending_verification',
  accepted: 'accepted',
};

/** 「显示已接受」开关的 localStorage 键（票面 §4.2 指定，值域是 `'true'`/`'false'`）。 */
export const SHOW_ACCEPTED_KEY = 'ahi.kanban.showAccepted.v1';

/** 空会话（无 `first_user_message`）的占位标题。 */
export const UNTITLED = '(未命名会话)';

/** 一张卡归属哪一列；`null` = 不属于任何**渲染中**的列（v1 有 deliverable 与
 *  后端可能新增的未知状态，前者由 `deliverableCount` 计数、后者由
 *  `unknownStateCount` 计数，都不在这里静默丢掉）。 */
export function columnOf(session: SessionSummary): KanbanColumn | null {
  const state = session.product_state;
  return (KANBAN_COLUMNS as readonly string[]).includes(state)
    ? (state as KanbanColumn)
    : null;
}

/** ISO 时刻 -> 毫秒；`null` / 不可解析一律 `null`（当作"没有这个键"处理，不猜一个 0）。
 *  用 `Date.parse` 而不是字符串比较：两种来源（`Z` 与 `+00:00`）的字典序与时间序不一致，
 *  字符串比较会在混合格式上静默排错。 */
function parseTime(iso: string | null): number | null {
  if (iso === null) return null;
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? null : parsed;
}

/** 降序、`null` 排最后（票面 AC-4：「该字段为 null 的卡排最后」）。 */
function descNullsLast(a: number | null, b: number | null): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return b - a;
}

/** 列内排序：`product_state_changed_at` 降序，`null` 最后；同键再按 `last_event_time` 降序。
 *
 *  `product_state_changed_at` 是**状态跃迁**时刻（同态事件不刷新它），所以"最近刚动的
 *  卡在最上"——与 Orca `AgentKanbanBoard.tsx:64-66` 的 `stateChangedAt` 同一条语义。
 *
 *  先算键再排序（Schwartzian）：比较器会被调 O(n log n) 次，逐次 `Date.parse` 是白做的
 *  重复解析。 */
export function sortColumn(cards: readonly SessionSummary[]): SessionSummary[] {
  return cards
    .map((card) => ({
      card,
      changed: parseTime(card.product_state_changed_at),
      last: parseTime(card.last_event_time),
    }))
    .sort((a, b) => descNullsLast(a.changed, b.changed) || descNullsLast(a.last, b.last))
    .map((entry) => entry.card);
}

/** 一卡一列的分组（Orca `AgentKanbanBoard.tsx:59-61` 同构：每张卡恰好 push 进一列）。
 *
 *  返回**全部**四列（含空列）——空列也要渲染（列头带 0 计数），这与"隐藏已接受"
 *  是两件事：可见性由调用方按开关决定，不在这里过滤，否则"开关打开时列里有几张卡"
 *  就无从得知（`accepted` 的计数会随开关变，那是两个真相）。 */
export function groupSessions(
  sessions: readonly SessionSummary[],
): Record<KanbanColumn, SessionSummary[]> {
  const grouped = Object.fromEntries(
    KANBAN_COLUMNS.map((column) => [column, [] as SessionSummary[]]),
  ) as Record<KanbanColumn, SessionSummary[]>;
  for (const session of sessions) {
    const column = columnOf(session);
    if (column !== null) grouped[column].push(session);
  }
  for (const column of KANBAN_COLUMNS) {
    grouped[column] = sortColumn(grouped[column]);
  }
  return grouped;
}

/** v1 未渲染的 deliverable 卡数（票面 AC-3：> 0 时看板顶部必须**说出来**，不许静默丢卡）。 */
export function deliverableCount(sessions: readonly SessionSummary[]): number {
  return sessions.filter((session) => session.product_state === UNRENDERED_STATE).length;
}

/** 后端新增的未知 `product_state` 的卡数（P4-4）。
 *
 *  这类卡既不进任何列（`columnOf` 回 `null`），又不被 `deliverableCount` 计数——
 *  后端加了新状态时它会**静默消失**。> 0 时看板顶部与 deliverable 警示并列说出来
 *  （不变量 #21 的"不许静默丢卡"同样覆盖它）。`deliverable` 本身是已知的未渲染态，
 *  不计入这里，避免两行警示重复报警。 */
export function unknownStateCount(sessions: readonly SessionSummary[]): number {
  return sessions.filter(
    (session) =>
      !(KANBAN_COLUMNS as readonly string[]).includes(session.product_state) &&
      session.product_state !== UNRENDERED_STATE,
  ).length;
}

/** 卡片标题：`first_user_message`，为空渲染占位（票面 §4.3）。 */
export function cardTitle(session: SessionSummary): string {
  return session.first_user_message ?? UNTITLED;
}

/** 搜索：按 `first_user_message` 做大小写不敏感子串过滤（票面 §4.6）。
 *  空查询 = 不过滤（返回原数组的副本，调用方据此跳过整轮重算）。 */
export function filterByTitle(
  sessions: readonly SessionSummary[],
  query: string,
): SessionSummary[] {
  const needle = query.trim().toLocaleLowerCase();
  if (needle.length === 0) return [...sessions];
  return sessions.filter((session) =>
    (session.first_user_message ?? '').toLocaleLowerCase().includes(needle),
  );
}

/** 读「显示已接受」开关。**纯函数 + 同步**（同 `lib/railArchive.ts` 的纪律：首帧就要
 *  按它决定渲染，不能在 effect 里补）。任何非 `'true'` 的值回默认 `false`——
 *  开关是布尔语义，没有"半个开关"可表达。 */
export function readShowAccepted(): boolean {
  try {
    return localStorage.getItem(SHOW_ACCEPTED_KEY) === 'true';
  } catch {
    // 隐私模式 / 存储被禁用：读不到按默认走，不让看板因为持久化失败而崩。
    return false;
  }
}

export function writeShowAccepted(value: boolean): void {
  try {
    localStorage.setItem(SHOW_ACCEPTED_KEY, value ? 'true' : 'false');
  } catch {
    // 同上：写失败不该让一次视图切换失败（本次切换已经生效在内存里）。
  }
}
