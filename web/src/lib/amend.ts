/** Composer 档位 → 提交契约字段的**单一映射点**。
 *
 * 背景：create（POST /api/sessions）与续聊（POST /api/sessions/{id}/messages）
 * 两条路径消费同一组 Composer 档位，但字段集不同——续聊的 amend 面不含
 * `permission_mode`（不在该端点的请求契约内）。此前 App.tsx 两处各写一遍
 * 「有值才带」展开，新增 amend 字段需要同时改两处（漂移风险）。
 *
 * 归一化归属（本模块的契约）：**丢弃空值是 api 层的事**。这里只做
 * camelCase → 契约字段名 的映射，不判断空值——`api.startSession` /
 * `api.sendMessage` 是「不传键 = 后端默认」这条语义的唯一执行点。
 * 这样「谁拥有契约」有唯一答案：api 层。 */

import type { SendMessagePayload, StartSessionPayload } from './api';

/** App 侧 Composer 档位（camelCase，与 React state 同名）。 */
export interface ComposerControls {
  model: string | null;
  permissionMode: string | null;
  agentProfile: string | null;
  reasoningEffort: string | null;
}

/** 续聊 amend 面：三项，不含 `permission_mode`（不在 /messages 契约内）。
 *  字段集直接取自 `SendMessagePayload` 的 Omit——请求契约增删字段时
 *  这个返回类型会跟着变，不会静默漂移。
 *
 *  #201：`context_providers` 随多选控件一并下线（UI 不再提供选择入口）。**不传键 =
 *  后端默认（全部已装配 provider）**，正是此前"未选"时的行为；`api.ts` 的
 *  `context_providers` 参数与后端契约一字未动，程序化调用仍可显式传值。 */
export function toAmendFields(
  c: ComposerControls,
): Omit<SendMessagePayload, 'content' | 'mode' | 'budget'> {
  return {
    model: c.model ?? undefined,
    agent_profile: c.agentProfile ?? undefined,
    reasoning_effort: c.reasoningEffort ?? undefined,
  };
}

/** 创建会话控制面：amend 三项 + `permission_mode`（该端点独有）。
 *
 * 返回类型里**没有** `context_providers`：函数体已不可能产出它（`toAmendFields` 不产、这里也不加），
 * 类型上留着会让调用方以为这是本函数承诺的契约——那正是本文件顶部警告的「契约归属漂移」。
 * （`toAmendFields` 的 `Omit` 里保留该键是另一回事：那是「整份 payload 的既有形状」。） */
export function toCreateControls(
  c: ComposerControls,
): Pick<StartSessionPayload, 'model' | 'permission_mode' | 'agent_profile' | 'reasoning_effort'> {
  return {
    ...toAmendFields(c),
    permission_mode: c.permissionMode ?? undefined,
  };
}

/** #426：新建会话的预算入口（常用三项）——Composer 的可选草稿 → `budget.run.*`，
 *  随启动 run 的 create 请求提交（#422 裁决：`budget.run` 属于**启动 run** 的请求；
 *  composer 提交恒为 launch=true，无冲突）。
 *
 *  空白 / 半截 / 非法草稿 → 该维不发键 = 后端按 Deployment 默认——「不设置预算的
 *  默认行为不变」，也不发一个必然被 422 拒掉的请求（turns/tokens 后端 `ge=1`；
 *  deadline 朴素时间/空串必 422）。
 *
 *  wire 形状（`RunBudgetRequest`，`src/agent_harness/web/app.py`，extra="forbid"）：
 *  `max_agent_turns_total` / `max_total_tokens` 正整数；`deadline_at` RFC 3339 UTC
 *  文本（datetime-local 的 naive-local 读数经 `Date`/`toISOString` 换算成 UTC 瞬时）。
 *  三项全空 → `undefined` = 整个 budget 键不发（提交载荷与无预算现状逐字节一致）。 */
export interface CreateBudgetDrafts {
  /** `budget.run.max_agent_turns_total` 草稿（输入框原样字符串）。 */
  turns: string | null;
  /** `budget.run.max_total_tokens` 草稿。 */
  totalTokens: string | null;
  /** `budget.run.deadline_at` 草稿——#536 起双形态：时长 token（`"30m"` / `"2h"` /
   *  自定义分钟 `"90m"`，提交时刻换算为绝对时刻）**或** `datetime-local` 原始值
   *  （如 `2026-10-01T12:30`，高级路径）。两形态天然互斥（regex 判形）。 */
  deadlineAt: string | null;
}

/** 正整数草稿判形：turns / total_tokens 共用（`/^\d+$/` + 安全整数 + ≥1；后端
 *  `ge=1`，0 与负数视同未设置而不发键）。 */
function parsePositiveIntDraft(draft: string | null): number | undefined {
  const trimmed = draft?.trim() ?? '';
  if (!/^\d+$/.test(trimmed)) return undefined;
  const value = Number.parseInt(trimmed, 10);
  if (!Number.isSafeInteger(value) || value < 1) return undefined;
  return value;
}

/** deadline 草稿 → RFC 3339 UTC 文本。无时区的 datetime-local 读数按**本地时区**
 *  解析（ES 对无偏移 date-time 的规定），`toISOString()` 归一到 UTC `Z` 形——
 *  后端 `parse_deadline_at` 收 Z/任意偏移并归一化，但拒朴素时间。 */
function parseDeadlineDraft(draft: string | null): string | undefined {
  const text = draft?.trim() ?? '';
  if (!text) return undefined;
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return undefined;
  return date.toISOString();
}

/** 时长 token 判形（#536 §2.2）：`"30m"` / `"2h"` / 自定义分钟 `"90m"` → 分钟数。
 *  与 turns/tokens 同一判形语义：0 / 半截 / 非法 / datetime-local 原始值 → null
 *  （视同"不是时长"，走高级路径或视同未设置——由调用方决定）。 */
export function parseDurationToken(draft: string | null | undefined): number | null {
  const match = /^(\d+)(m|h)$/.exec(draft?.trim() ?? '');
  if (!match) return null;
  const amount = Number.parseInt(match[1] ?? '', 10);
  if (amount < 1) return null;
  return match[2] === 'h' ? amount * 60 : amount;
}

/** deadline 草稿 → 绝对时刻（RFC 3339 UTC）——#536 的**换算唯一执行点**：
 *  提交时刻（`toCreateBudget`）与预览渲染时刻（`budgetUi.deadlinePreview`）消费
 *  同一实现，时长档与 datetime 档的换算不可能漂移成两套。
 *  - 时长 token（`/^(\d+)(m|h)$/`）→ `now + 时长`（`now` 缺省取当前时刻）；
 *  - 其余 → datetime-local 原路径（语义逐字节不变）；
 *  - 空 / 0 时长 / 非法 → undefined（不发键）。 */
export function resolveDeadlineDraft(
  draft: string | null | undefined,
  now: Date = new Date(),
): string | undefined {
  const minutes = parseDurationToken(draft);
  if (minutes !== null) {
    return new Date(now.getTime() + minutes * 60_000).toISOString();
  }
  return parseDeadlineDraft(draft ?? null);
}

export function toCreateBudget(drafts: CreateBudgetDrafts): StartSessionPayload['budget'] {
  const turns = parsePositiveIntDraft(drafts.turns);
  const totalTokens = parsePositiveIntDraft(drafts.totalTokens);
  // #536：换算唯一执行点（时长档 + datetime 高级路径同一实现，预览侧复用）。
  const deadlineAt = resolveDeadlineDraft(drafts.deadlineAt);
  if (turns === undefined && totalTokens === undefined && deadlineAt === undefined) {
    return undefined;
  }
  // 键按固定顺序构造：wire 字节可复现（「不填 = 与现状逐字节一致」的反向承诺）。
  const run: {
    max_agent_turns_total?: number;
    max_total_tokens?: number;
    deadline_at?: string;
  } = {};
  if (turns !== undefined) run.max_agent_turns_total = turns;
  if (totalTokens !== undefined) run.max_total_tokens = totalTokens;
  if (deadlineAt !== undefined) run.deadline_at = deadlineAt;
  return { run };
}
