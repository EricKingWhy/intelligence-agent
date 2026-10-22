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

/** 事实类型的中文名（#1445 / F-04）——**单一 owner**，与 `SCOPE_LABELS` / `FACT_TYPE_DOTS`
 *  同处同模式。
 *
 *  口径出处：issue #1445 与 `~/workspace/overnight-test/walk-p2-fixes/f04-wording.md`
 *  （逐枚举定名，逐字照用；`constraint` / `authorization` / `evidence_ref` 为既有口径，
 *  `user_goal` / `work_boundary` / `failed_approach` 为按枚举字面与既有词根新拟）；
 *  补充 7 类定名见同目录 `f04-wording-addendum.md`（双轴审查 F1 裁决）。
 *  原始枚举是机器码，不是给用户看的筛选标签——chips 与行内类型都走 `factTypeLabel`。
 *
 *  键集 = 后端可产出的**已知**全集 13 类（derive `_USER_FACT_TYPES` 九值 +
 *  `work_boundary` / `failed_approach` / `evidence_ref` / `unresolved_operation`）。
 *  其中 chips 仍只六类（`TYPE_OPTIONS` 不动）；多出的 7 类只作行内 / 继承区显示名，
 *  不进筛选 chips（无 chip 可筛是预存在行为，非本票缺陷）。 */
export const FACT_TYPE_LABELS: Record<string, string> = {
  constraint: '约束',
  user_goal: '用户目标',
  authorization: '授权',
  work_boundary: '工作边界',
  failed_approach: '失败尝试',
  evidence_ref: '证据引用',
  authorization_revocation: '授权撤销',
  unresolved_operation: '待对账操作',
  user_instruction: '用户指令',
  acceptance_criterion: '验收标准',
  exact_identifier: '精确标识',
  confirmed_decision: '已确认决策',
  task_progress: '任务进度',
};

/** 未知 / 未来枚举的兜底（口径文件「未知值兜底」）。依据 = `modelAvailability.ts:20-40`
 *  的模式：机器码**绝不原样打给用户**，未知码只给保守通用词、不猜具体含义；**不是**
 *  `permission.ts` `decisionLabel` 那种「未知值原样返回」——chips 是用户筛选标签，
 *  原样回显 snake_case 正是 #1445 本身的 bug 形状。筛选仍按原始值匹配（只改显示名）。
 *
 *  `hasOwnProperty` 守卫（同 `permission.ts` `decisionLabel`）：`Record<string, string>`
 *  继承 `Object.prototype`，`__proto__` / `constructor` / `toString` 会命中原型成员而非
 *  `undefined`——不守卫则「任意未知 string → 其他类型」的契约在这些键上不成立。 */
export function factTypeLabel(type: string): string {
  return Object.prototype.hasOwnProperty.call(FACT_TYPE_LABELS, type)
    ? FACT_TYPE_LABELS[type]
    : '其他类型';
}

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
