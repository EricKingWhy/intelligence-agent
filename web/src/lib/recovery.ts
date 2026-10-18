/** #357 W-13 恢复 UI 的纯函数层（无 React / 无 DOM）。
 *
 *  本文件**只保留**仍然活着的两半：
 *  - 恢复列表（`RecoveryListPanel`）用的进度文件版本文案；
 *  - 待裁决横幅 / 恢复冲突条用的两层文案（人话 + 折叠层协议原文）。
 *
 *  `#1327` 删除了第三半——「UNKNOWN 操作人工裁决」的四选一与 wire 载荷组装
 *  （`DECISION_OPTIONS` / `assembleDecisions` / `initialDrafts` / `probeFact` /
 *  `humanizeToolName` / `SOURCE_PRESETS`）。裁决不再由用户做：后端按操作类型自动
 *  裁决（只读 + `replay_safe` ⇒ 重跑；其余 ⇒ 结清不重跑），前端因此没有可组装的
 *  裁决载荷。删除而不是保留，是因为**没有消费方的纯函数**只会腐烂成第二套真相
 *  （不变量 #22 的同向纪律）。
 *
 *  边界（不变量 #22）：本文件不发明任何裁决事实——横幅文案逐字来自后端载荷。
 */

import type { InterruptedProgressVersion } from './api';

/** 待裁决横幅的**第一层：人话**（#1030 缺陷③ / AC2，Kubernetes reason/message 双层的 ADAPT）。
 *
 *  后端 `RecoveryConflict` 的 message 内嵌 `POST /api/sessions/{id}/recover`、
 *  `tool_call_id`、`verdict ∈ …` 等协议原文——它是写给调用方的，不是写给用户的。
 *  此前前端把 detail 逐字当文案（`useSession.ts` 的 409 分支 → `.app-error`），
 *  最终用户既不知道什么是 tool_call_id，也无法手写 POST。
 *
 *  这里给的是**如实告知**的那一句：说清"有几个操作还没结清"，并点明**系统会自己
 *  处理**（`#1327` 起按操作类型自动裁决：只读 + `replay_safe` ⇒ 重跑，其余 ⇒
 *  结清不重跑）。文案刻意不再写"需要你裁决"——那是被删除的弹窗的承诺，留着就是
 *  骗人（PRODUCT 原则 3）。协议原文不放这里，它进折叠层（`techDetail`）。
 *
 *  `count` 为 `null` = 只知道"有待裁决"但拿不到条数（例如 409 的 detail 是纯文本、
 *  没带结构化清单）。此时**不编造数字**，说"有操作"而不是"有 1 个操作"。 */
export function adjudicationHeadline(count: number | null): string {
  if (count === null) return '这个会话有操作的结果还没结清——下次恢复会按操作类型自动处理。';
  if (count === 1) return '这个会话有 1 个操作的结果还没结清——下次恢复会按操作类型自动处理。';
  return `这个会话有 ${count} 个操作的结果还没结清——下次恢复会按操作类型自动处理。`;
}

/** 技术详情（折叠层）。后端原文**逐字保留**：它包含 tool_call_id / 端点 / verdict 枚举，
 *  是排查与手工结清的唯一依据——翻译掉就等于把它藏起来（AC2 要求"只在折叠层出现"，
 *  不是"消失"）。`message` 为空时不渲染折叠层（没有可展开的内容）。 */
export function adjudicationTechDetail(message: string | null): string | null {
  const text = (message ?? '').trim();
  return text === '' ? null : text;
}

/** 进度文件版本的诚实文案。可读 → 版本 + 事件序号；缺失/不可读/不匹配各自
 *  如实区分，**绝不写「最新」**（文件读不到就是读不到）。 */
export function formatProgressVersion(progress: InterruptedProgressVersion): string {
  if ('schema_version' in progress) {
    return `版本 ${progress.schema_version} · 进度事件 #${progress.source_event_seq}`;
  }
  if (progress.status === 'missing') return '未知（进度文件缺失）';
  if (progress.status === 'unreadable') return '未知（进度文件不可读）';
  return '未知（版本不匹配）';
}

/** 供 UI 上色/加标记：可读 = ok，其余 = unknown（unknown 不得被渲染成安全绿）。 */
export function progressTone(progress: InterruptedProgressVersion): 'ok' | 'unknown' {
  return 'schema_version' in progress ? 'ok' : 'unknown';
}
