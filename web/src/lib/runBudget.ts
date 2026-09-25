/** 预算暂停的展示事实（`#312` T4）——从 `RunPausedInfo`（事件真值）派生的**纯函数**。
 *
 *  为什么单列一个模块：CLI（`agent_harness.cli.render_pause_block` / `_pause_facts`）与
 *  Web 必须显示**同一份事实**（票面 AC：「CLI and Web display the same pause reason,
 *  consumed/limit values, version and continuation after refresh」）。这里逐条镜像 CLI
 *  的取值口径，包括"缺数字 = unavailable，**永不**用 0 顶替"（`11 §6.1`）与
 *  "unlimited 不是 0"。
 *
 *  这里**不做**任何会话状态判断——输入就是投影出来的暂停事实，输出是要渲染的文本
 *  片段与数字。渲染组件（`components/PausedBanner.tsx`）只负责排版。 */

import type { RunPausedInfo } from '../types';

/** 后端暂停判据里的两个常量（**只做展示口径，判定权威在后端**）。
 *
 *  `RESERVED_CLOSEOUT_TURNS` 镜像 `run_budget.RESERVED_CLOSEOUT_TURNS`：暂停点在
 *  `consumed + 预留 >= ceiling`，所以恢复至少要留出"一个可接纳 turn + 一次 closeout
 *  预留" ⇒ 最小合法 ceiling = `consumed + RESERVED + 1`（后端 `resume_ceiling_ok`
 *  的 `ceiling > consumed + RESERVED`）。前端拿它**提示**（省一次必然 409 的往返），
 *  真正的拒绝在后端——两边口径若分叉，以后端为准，改这里对齐。 */
export const RESERVED_CLOSEOUT_TURNS = 1;

/** 恢复请求的最小合法绝对 ceiling（绝对**值**，不是增量）。 */
export function minResumeCeiling(consumedTurns: number): number {
  return consumedTurns + RESERVED_CLOSEOUT_TURNS + 1;
}

export interface PauseFacts {
  consumed: number;
  /** 绝对 ceiling；null = 未配（UI 文案 "unlimited"，不是 0）。 */
  ceiling: number | null;
  /** ceiling − consumed（下限 0）；ceiling 缺失时为 null。 */
  remaining: number | null;
  localFuseTurns: number | null;
  localFuseSource: string;
  closeoutSource: string;
  version: number;
  /** 命中维度的人话标签（未知维度原样回显，不编名字）。 */
  dimensionLabel: string;
  /** 恢复所需的最小绝对 ceiling（见 `minResumeCeiling`）。 */
  minResumeCeiling: number;
}

/** 命中的 ceiling 维度 → 标签。取值域来自后端 `run_budget.TRIGGER_*`；未知值原样
 *  回显（不猜、也不显示成"未知维度"——原码本身对排查有用）。 */
export function dimensionLabel(dimension: string): string {
  switch (dimension) {
    case 'run.max_agent_turns_total':
      return 'run 累计轮次到顶（run.max_agent_turns_total）';
    case 'local.max_agent_turns':
      return '单次执行保险丝到顶（local.max_agent_turns）';
    default:
      return dimension || '预算维度未声明';
  }
}

export function pauseFacts(paused: RunPausedInfo): PauseFacts {
  const ceiling = paused.run_limit;
  return {
    consumed: paused.consumed_agent_turns,
    ceiling,
    remaining: ceiling === null ? null : Math.max(ceiling - paused.consumed_agent_turns, 0),
    localFuseTurns: paused.local_fuse?.max_agent_turns ?? null,
    localFuseSource: paused.local_fuse?.source ?? '',
    closeoutSource: paused.closeout_source,
    version: paused.version,
    dimensionLabel: dimensionLabel(paused.trigger_dimension),
    minResumeCeiling: minResumeCeiling(paused.consumed_agent_turns),
  };
}

/** 绝对 ceiling 输入框的预校验（**只为省一次必然 409 的往返**，不是规则来源）。
 *
 *  返回 null = 可以提交；返回字符串 = 就地提示的原因。
 *  判据与后端 `resume_ceiling_ok` 逐字一致（`ceiling > consumed + 1`）——前端多一条
 *  自己的规则就会与后端分叉，所以这里刻意只有这一条，且措辞指向是同一个数。 */
export function ceilingDraftError(paused: RunPausedInfo, draft: string): string | null {
  const text = draft.trim();
  const min = minResumeCeiling(paused.consumed_agent_turns);
  if (!text) return `请填绝对 ceiling（至少 ${min}）`;
  if (!/^\d+$/.test(text)) return 'ceiling 必须是正整数';
  const value = Number(text);
  if (!Number.isSafeInteger(value) || value < 1) return 'ceiling 必须是正整数';
  if (value < min) {
    return `ceiling 必须大于已消耗 ${paused.consumed_agent_turns} + ${RESERVED_CLOSEOUT_TURNS}（至少 ${min}），否则恢复后立刻会再次暂停`;
  }
  return null;
}

/** 合法输入 → 提交用的整数；非法返回 null（调用方据此禁用按钮）。 */
export function ceilingDraftValue(paused: RunPausedInfo, draft: string): number | null {
  if (ceilingDraftError(paused, draft) !== null) return null;
  return Number(draft.trim());
}

/** continuation 的分段标签（`03 §3.4` 四键；与 CLI `_continuation_lines` 同序）。 */
export const CONTINUATION_SECTIONS = [
  { key: 'completed', label: '已完成' },
  { key: 'remaining', label: '剩余' },
  { key: 'blockers', label: '阻塞' },
] as const;
