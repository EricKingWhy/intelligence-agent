/** #536（A 输入形态）的预算 UI 纯函数层：tokens 档位目录、时长档目录、
 *  trigger 摘要、deadline 预览。**只做展示侧判读**，提交 wire 的判形/换算
 *  唯一执行点仍是 `amend.ts`（`parsePositiveIntDraft` / `resolveDeadlineDraft`）——
 *  本模块 import 它们复用，绝不复制第二份判形（否则 trigger 摘要与真实提交
 *  各说各话，正是票面要消灭的形态）。
 *
 *  票面锚点：docs/design/t435-budget-ui-interaction-design.md
 *  §2.1 tokens 档位 100k/250k/500k/1M/自定义（不做 token→次数换算）；
 *  §2.2 deadline 时长档 30m/1h/2h/4h/自定义时长，datetime-local 保留为高级路径，
 *  预览「≈ 10-02 15:30 截止（2 小时后）」，过期 → 「已过期」（仅前置提示）；
 *  §2.5 默认值用 placeholder / 「默认」表达。 */

import { parseDurationToken, resolveDeadlineDraft } from './amend';

/** tokens 档位（§2.1）。`id` 即 OptionPicker 的 value；draft 命中档位时存的就是
 *  数字字符串（`'500000'`），toCreateBudget 原样消费。 */
export const TOKEN_TIERS: ReadonlyArray<{ id: string; label: string; value: number }> = [
  { id: '100000', label: '100k', value: 100_000 },
  { id: '250000', label: '250k', value: 250_000 },
  { id: '500000', label: '500k', value: 500_000 },
  { id: '1000000', label: '1M', value: 1_000_000 },
];

/** tokens「自定义」档哨兵（OptionPicker 选中它 → 展开数字输入框，不关闭面板）。 */
export const TOKEN_CUSTOM_ID = '__custom_tokens__';

/** deadline 时长档（§2.2）。`token` 直接作为 deadline 草稿写入（`'30m'` / `'2h'`）。 */
export const DURATION_TIERS: ReadonlyArray<{ id: string; label: string; token: string }> = [
  { id: '30m', label: '30 分钟', token: '30m' },
  { id: '1h', label: '1 小时', token: '1h' },
  { id: '2h', label: '2 小时', token: '2h' },
  { id: '4h', label: '4 小时', token: '4h' },
];

/** deadline「自定义时长」档哨兵（选中 → 展开分钟输入框）。 */
export const DURATION_CUSTOM_ID = '__custom_duration__';

/** tokens 草稿 → 档位 id：精确命中档位值 → 该档；其他合法正整数 → 自定义哨兵；
 *  空 / 非法 → null（档位显示"未选"）。 */
export function tokensTierId(draft: string): string | null {
  const trimmed = draft.trim();
  const tier = TOKEN_TIERS.find((t) => t.id === trimmed);
  if (tier) return tier.id;
  return /^\d+$/.test(trimmed) && Number.parseInt(trimmed, 10) >= 1 ? TOKEN_CUSTOM_ID : null;
}

/** deadline 草稿 → 时长档 id：命中固定档 → 该档；其他合法时长 → 自定义哨兵；
 *  空 / datetime-local / 非法 → null。 */
export function durationTierId(draft: string): string | null {
  const trimmed = draft.trim();
  const tier = DURATION_TIERS.find((t) => t.token === trimmed);
  if (tier) return tier.id;
  return parseDurationToken(trimmed) !== null ? DURATION_CUSTOM_ID : null;
}

/** tokens 数值 → 紧凑标签（trigger 摘要用）：≥1M → `x.xM`、≥1k → `xk`、否则原数；
 *  一位小数去尾零（1_500_000 → '1.5M'，2_000_000 → '2M'）。非法 → null。 */
export function tokensLabel(draft: string): string | null {
  const trimmed = draft.trim();
  if (!/^\d+$/.test(trimmed) || Number.parseInt(trimmed, 10) < 1) return null;
  const value = Number.parseInt(trimmed, 10);
  const compact = (n: number): string => {
    const rounded = String(parseFloat(n.toFixed(1)));
    return rounded;
  };
  if (value >= 1_000_000) return `${compact(value / 1_000_000)}M`;
  if (value >= 1_000) return `${compact(value / 1_000)}k`;
  return String(value);
}

/** 本地时刻 → `MM-DD HH:mm`（同年）或 `YYYY-MM-DD HH:mm`（跨年，消歧）。 */
function localStamp(date: Date, now: Date): string {
  const pad = (n: number): string => String(n).padStart(2, '0');
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`;
  const md = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return date.getFullYear() === now.getFullYear() ? `${md} ${hm}` : `${date.getFullYear()}-${md} ${hm}`;
}

/** 时长 token → 相对语（`'2h'` → '2 小时后'、`'30m'` → '30 分钟后'）；非时长 → null。 */
function relativeText(draft: string): string | null {
  const match = /^(\d+)(m|h)$/.exec(draft.trim());
  if (!match) return null;
  return match[2] === 'h' ? `${match[1]} 小时后` : `${match[1]} 分钟后`;
}

/** deadline 草稿 → 预览行读数（§2.2）。
 *  - 空 / 换算不出绝对时刻（非法）→ null（不渲染预览行）；
 *  - 绝对时刻早于 `now` → 「已过期」（渲染层配 `--danger`；时长档 resolve 后
 *    恒不早于 now，故只有 datetime 高级路径会真的过期）；
 *  - 否则 `≈ <本地时刻> 截止（<相对语>）`——相对语仅时长档有。 */
export function deadlinePreview(
  draft: string,
  now: Date,
): { text: string; expired: boolean } | null {
  const resolved = resolveDeadlineDraft(draft, now);
  if (resolved === undefined) return null;
  const date = new Date(resolved);
  if (date.getTime() < now.getTime()) return { text: '已过期', expired: true };
  const relative = relativeText(draft);
  const suffix = relative ? `（${relative}）` : '';
  return { text: `≈ ${localStamp(date, now)} 截止${suffix}`, expired: false };
}

/** Composer trigger 的常显摘要（§2.5「默认值用 placeholder 表达」）：
 *  全空 → 「默认」；否则 turns 部分恒在（`turns N` / `turns 默认`），
 *  tokens / deadline 部分设了才出，` · ` 连接。判形复用映射层：tokens 合法性
 *  用 `tokensLabel`（同一 `/^\d+$/` + ≥1 口径），deadline 用档位反查 + 预览换算。 */
export function budgetSummary(turns: string, tokens: string, deadline: string): string {
  const turnsTrimmed = turns.trim();
  const turnsSet = /^\d+$/.test(turnsTrimmed) && Number.parseInt(turnsTrimmed, 10) >= 1;
  const tokensText = tokensLabel(tokens);
  const isDuration = durationTierId(deadline) !== null;
  // datetime 高级路径：能换算出绝对时刻才算设置（与提交判形同一执行点）。
  const resolved = resolveDeadlineDraft(deadline);
  const deadlineText = isDuration
    ? deadline.trim()
    : resolved !== undefined
      ? localStamp(new Date(resolved), new Date())
      : null;
  if (!turnsSet && tokensText === null && deadlineText === null) return '默认';
  const parts = [`turns ${turnsSet ? turnsTrimmed : '默认'}`];
  if (tokensText !== null) parts.push(tokensText);
  if (deadlineText !== null) parts.push(deadlineText);
  return parts.join(' · ');
}
