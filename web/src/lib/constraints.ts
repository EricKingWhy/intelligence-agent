/** 约束管理页（#1190 / #1176 Phase 1）的纯展示助手。
 *
 *  放在 `lib/` 而不是组件模块内：组件模块只导出组件（react-refresh 的
 *  `only-export-components`），这些纯函数直接单测、也可被别处复用。 */

import type { ConstraintScope } from './api';
import { formatDateTime } from './format';

/** 作用域三选一的中文标签（#1191 §四.1 / §五.2）——**单一 owner**。
 *
 *  登记 pill（`Composer`）与三组眉标（`ConstraintsTab`）共用，避免同一份词汇在
 *  两处各自声明后漂移（审查 finding S-b3）。 */
export const SCOPE_LABELS: Record<ConstraintScope, string> = {
  session: '本会话',
  project: '本项目',
  global: '我的全局',
};

/** 事实类型圆点固定色（#1190 issue §四.4）；未知类型回落到 ink-subtle 灰。
 *  单一 owner 放 `lib/`——`ConstraintsTab` 与 #1192 子详情「约束继承」区共用，
 *  避免同一份类型色板两处漂移。 */
export const FACT_TYPE_DOTS: Record<string, string> = {
  constraint: 'var(--cn-type-constraint)',
  user_goal: 'var(--cn-type-user-goal)',
  authorization: 'var(--cn-type-authorization)',
  work_boundary: 'var(--cn-type-work-boundary)',
  failed_approach: 'var(--cn-type-failed-approach)',
  evidence_ref: 'var(--cn-type-evidence-ref)',
};

/** ISO 时间 → 全站统一 `YYYY-MM-DD HH:mm`（本地时区）；坏值原样返回，不猜。 */
export function formatFactTime(iso: string | null): string {
  if (!iso) return '';
  return formatDateTime(iso) ?? iso;
}

/** 操作人展示：本地信任身份的哨兵 `local` → 「本人操作」，其余原样（真实优先）。 */
export function operatorLabel(revokedBy: string | null): string {
  if (!revokedBy) return '';
  return revokedBy === 'local' ? '本人操作' : revokedBy;
}
