/** 项目分组与账本重排的纯函数（WS-5 / #155）。
 *
 *  **单一真相**（不变量 #22）：分组结果只从两个后端真相源派生——
 *  `GET /api/sessions` 的 `SessionSummary.workspace` 与 `GET /api/projects` 的
 *  `session_ids`（账本手工序）。前端不维护"项目有哪些会话"的第二份名单：
 *  它只做投影，投影错了刷新一次就自愈。
 *
 *  两条被刻意选择的失效方向：
 *  1. **绝不因为分组而丢掉一行**。未出现在任何账本里的会话（含 `workspace` 非
 *     null 但已不在账本、引用已失效项目的孤儿行、后端列表比账本新的竞态）一律
 *     落到「未分组」区——丢一行比错放一行糟得多（WS-5 AC2 的底线）。
 *  2. **项目一条也不少**。`GET /api/projects` 成功返回的项目全部渲染，哪怕
 *     账本是空的（刚注册、还没会话）或目录已被移走（`missing-dir` 只上报，不改
 *     记录）——隐藏一个用户注册过的项目等于偷偷撤销他的操作。
 */

import type { Project, SessionSummary } from '../types';

export interface ProjectGroup {
  project: Project;
  /** 该项目账本里**确实存在**的会话摘要，顺序 = 账本手工序（不按活动时间重排）。 */
  sessions: SessionSummary[];
  /** 账本里有 id、但列表里没有对应会话的条数（日志被删 / 成员资格过滤）。
   *  如实上报给用户，不静默吞掉——`GET /api/sessions?workspace_id=` 对这种情况
   *  也返回 `[]`，两边口径一致。 */
  missing: number;
}

export interface RailModel {
  groups: ProjectGroup[];
  /** 不属于任何已渲染项目的会话（保持后端给的活动时间序）。 */
  ungrouped: SessionSummary[];
}

/** 把会话列表投影成「项目 → 会话」+「未分组」。 */
export function buildRailModel(
  sessions: readonly SessionSummary[],
  projects: readonly Project[],
): RailModel {
  const byId = new Map(sessions.map((s) => [s.session_id, s]));
  // 全局占用表：同一会话理论上只属于一个项目，但脏数据（或两次刷新之间的竞态）
  // 可能让两个账本都点到它——只渲染第一次出现的位置，第二处计为 missing。
  const claimed = new Set<string>();

  const groups = projects.map((project): ProjectGroup => {
    const rows: SessionSummary[] = [];
    let missing = 0;
    for (const id of project.session_ids) {
      if (claimed.has(id)) {
        missing += 1;
        continue;
      }
      claimed.add(id);
      const summary = byId.get(id);
      if (summary) rows.push(summary);
      else missing += 1;
    }
    return { project, sessions: rows, missing };
  });

  return {
    groups,
    ungrouped: sessions.filter((s) => !claimed.has(s.session_id)),
  };
}

/** `POST /api/projects/{id}/sessions/{sid}/order` 的请求体语义：
 *  DOM `insertBefore` —— 把 `sid` 移到 `before` 之前，`null` = 追加到队尾。
 *  类型上区分「要发请求」与「无需请求」。

 *  - `null` → 目标位置 == 当前位置：**不要发请求**（发了也是把同一份账本重写一遍，
 *    多一次写库 + 一次 409/空转的机会）；
 *  - `{ before }` → 调用 reorder 端点。 */
export type ReorderAnchor = { before: string | null } | null;

/** 账本里上移/下移一格的锚点。
 *
 *  `ids` 是账本现状（`project.session_ids`）。返回 `null` 表示已在边界（首条上移 /
 *  末条下移）——UI 据此禁用按钮，**不是**"不支持的语义"。
 *
 *  为什么"下移"常常给出 `before=null`：`[a, s, b]` 里 s 下移一格的落点是队尾，
 *  而 insertBefore(null) 正是追加队尾；`[a, s, b, c]` 里则要 insertBefore(c)。 */
export function moveAnchor(
  ids: readonly string[],
  sessionId: string,
  dir: 'up' | 'down',
): ReorderAnchor {
  const i = ids.indexOf(sessionId);
  if (i < 0) return null;
  if (dir === 'up') {
    if (i === 0) return null;
    return { before: ids[i - 1] };
  }
  if (i === ids.length - 1) return null;
  const after = ids[i + 2];
  return { before: after === undefined ? null : after };
}

/** 拖放落点 → 锚点。`before` 是"放在谁前面"，`null` 表示放到队尾（尾部放置区）。
 *
 *  两种情况判为无需请求：目标就是自己；以及目标位置与当前位置**等价**——
 *  `insertBefore(下一个兄弟)` 的结果与现状逐元素相同（`[a, b, c]` 把 b 放到 c 前
 *  还是 `[a, b, c]`），发出去只会白白重写一次账本。 */
export function dropAnchor(
  ids: readonly string[],
  sessionId: string,
  before: string | null,
): ReorderAnchor {
  const i = ids.indexOf(sessionId);
  if (i < 0) return null;
  if (before !== null && before === sessionId) return null;
  if (before === null) return i === ids.length - 1 ? null : { before: null };
  const j = ids.indexOf(before);
  if (j < 0) return null; // 锚点不在本项目账本里（后端会 409）——不猜、不发
  if (j === i + 1) return null; // 已经在它前面了
  return { before };
}
