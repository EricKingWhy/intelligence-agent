/** #1306 看板未读 / ack 的**纯函数层**（无 DOM、无 React，同 `lib/kanban.ts` 的分工）。
 *
 *  来源（抄优先，Orca @ `de1f6e3d8176beabdb189e71e6f3047bbe0edfad`，MIT）：
 *  - 判据形状：`src/renderer/src/attention/agent-attention-acknowledgement.ts:26-48`
 *    的 `isAgentTurnUnacknowledged(acknowledgedAt, turnStartedAt)` ——
 *    `turnStartedAt != null && acknowledgedAt < turnStartedAt`。
 *  - 比较键选型的上游原话（同文件 `:45-49`）：
 *    "Why compare stateStartedAt (not updatedAt): same-state pings must not re-trigger
 *    an ack, matching the is-unvisited rule the workspace card uses."
 *    **本仓逐字对应**：比 `product_state_changed_at`，**绝不比 `last_event_time`**
 *    ——后者每条事件都推进（agent 每说一句话），拿它当键会让已读卡立刻重新变粗、
 *    闪个不停，正是上游修掉的那个 bug。
 *
 *  **适配点**（票面「实现原则」允许改的只有这些）：
 *  - 上游按 `acknowledgedAt` 是数字毫秒、存磁盘；本仓是 ISO8601 字符串、存
 *    localStorage（键 `ahi.kanban.ack.v1`）。清浏览器存储 = 全部重新变未读，
 *    这是可接受的取舍（不是第二套真相：ack 是纯本地读态，不参与业务投影）。
 */

import type { SessionSummary } from '../types';

/** ack 表的 localStorage 键（票面 §4.4 指定）。 */
export const KANBAN_ACK_KEY = 'ahi.kanban.ack.v1';

/** `{ "<session_id>": "<ISO8601>" }`。 */
export type AckMap = Record<string, string>;

/** ISO 时刻 -> 毫秒；不可解析一律 `null`（当作"没有这个键"，不猜成 0）。
 *  与 `lib/kanban.ts::parseTime` 同口径——两处各自私有，避免为一个两行函数拉公共模块。 */
function parseTime(iso: string | null | undefined): number | null {
  if (iso === null || iso === undefined) return null;
  const parsed = Date.parse(iso);
  return Number.isNaN(parsed) ? null : parsed;
}

/** 这张卡是否**未读**：当前状态开始于上次 ack 之后。
 *
 *  - `product_state_changed_at === null`（含 backlog 空串、不可解析）⇒ 没有"状态开始
 *    时刻"可读，永不未读（上游 `turnStartedAt != null` 同一条短路）。
 *  - ack 缺失 / 损坏 ⇒ 未读：宁可多提醒一次，也不静默吞掉一个待看的卡。
 *
 *  **已接受的偏离**：损坏 ack 按未读，与票面公式字面的 `Date.parse(ack ?? 0) < changed`
 *  （损坏字符串得 NaN ⇒ 已读）不一致——这是故意的 fail-safe 方向，不是公式抄错；
 *  下次点击即被新鲜 ISO 覆盖。行为由单测"ack 值损坏"一条钉住。 */
export function unread(session: SessionSummary, ack: AckMap): boolean {
  const changed = parseTime(session.product_state_changed_at);
  if (changed === null) return false;
  const acknowledged = parseTime(ack[session.session_id]);
  return acknowledged === null || acknowledged < changed;
}

/** 读 ack 表。损坏 JSON / 非对象 / 值不是字符串：类型守卫逐条丢弃（不抛、不清库）。 */
export function readAck(): AckMap {
  try {
    const raw = localStorage.getItem(KANBAN_ACK_KEY);
    if (raw === null) return {};
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) return {};
    const ack: AckMap = {};
    for (const [sessionId, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof value === 'string') ack[sessionId] = value;
    }
    return ack;
  } catch {
    // 隐私模式 / 损坏 JSON：读不到按空表走，不让看板因为持久化失败而崩。
    return {};
  }
}

/** 写 ack 表（写失败静默：本次 ack 已在内存生效，不该让一次点击失败）。 */
export function writeAck(ack: AckMap): void {
  try {
    localStorage.setItem(KANBAN_ACK_KEY, JSON.stringify(ack));
  } catch {
    /* 同上。 */
  }
}

/** 把一张卡标成已读：与落盘表合并后写回、返回新表（**不改入参**）。
 *
 *  合并（而不是拿内存表全量写回）是为了修同 tick 连点/跨 tab 丢 ack：调用方的
 *  `ack` 是渲染闭包里的旧快照，直接 spread 写回会覆盖掉别处刚写入的条目。
 *  仍有 TOCTOU 窗口（read 与 write 之间），但把它从"必丢"压到"跨 tab 同瞬"。
 *
 *  `at` 可注入是为了单测能钉住写进去的值；调用方不传 = 现在。 */
export function markAcknowledged(
  sessionId: string,
  ack: AckMap,
  at: Date = new Date(),
): AckMap {
  const next: AckMap = { ...readAck(), ...ack, [sessionId]: at.toISOString() };
  writeAck(next);
  return next;
}
