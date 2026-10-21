/** #1445 / F-04：「我的要求」事实类型的中文名口径单测。
 *
 *  原始枚举（snake_case）是机器码，不是给终端用户看的筛选标签。中文名与出处以
 *  `~/workspace/overnight-test/walk-p2-fixes/f04-wording.md`（issue #1445）为准，
 *  逐字照用；未知值兜底沿用 `modelAvailability.ts` 的模式——**绝不把机器码原样
 *  打给用户**（不是 `decisionLabel` 那种「未知值原样返回」）。 */

import { describe, expect, it } from 'vitest';
import { FACT_TYPE_LABELS, factTypeLabel } from './constraints';

/** 口径文件「逐枚举定名」表，顺序 = #1176 PRD:52 / 既有 `TYPE_OPTIONS` 顺序（六类），
 *  其后为补充 7 类——后端可产出的**已知**全集，chips 仍只六类（`TYPE_OPTIONS` 不动）。 */
const CANONICAL: readonly (readonly [string, string])[] = [
  ['constraint', '约束'],
  ['user_goal', '用户目标'],
  ['authorization', '授权'],
  ['work_boundary', '工作边界'],
  ['failed_approach', '失败尝试'],
  ['evidence_ref', '证据引用'],
  ['authorization_revocation', '授权撤销'],
  ['unresolved_operation', '待对账操作'],
  ['user_instruction', '用户指令'],
  ['acceptance_criterion', '验收标准'],
  ['exact_identifier', '精确标识'],
  ['confirmed_decision', '已确认决策'],
  ['task_progress', '任务进度'],
];

describe('factTypeLabel', () => {
  it.each(CANONICAL)('%s → %s（口径文件定名）', (type, label) => {
    expect(factTypeLabel(type)).toBe(label);
  });

  it('映射键集与 13 类已知全集恰好相等（防漏、防多）', () => {
    expect(Object.keys(FACT_TYPE_LABELS).sort()).toEqual(CANONICAL.map(([type]) => type).sort());
  });

  it.each(['future_type', '', 'CONSTRAINT', '__proto__', 'constructor', 'toString'])(
    '未知值 %j → 「其他类型」（不原样回显机器码）',
    (type) => {
      expect(factTypeLabel(type)).toBe('其他类型');
    },
  );
});
