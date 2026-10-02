/** budgetUi 纯函数契约测试——#536（A 输入形态）的档位目录 / 摘要 / 预览三件套。
 *
 *  票面锚点（docs/design/t435-budget-ui-interaction-design.md §2.1/§2.2/§2.5）：
 *  - tokens 档位 100k / 250k / 500k / 1M / 自定义；**不做** token→次数换算；
 *  - deadline 时长档 30m / 1h / 2h / 4h / 自定义时长；datetime-local 保留为高级路径；
 *  - 预览行「≈ 10-02 15:30 截止（2 小时后）」；过期 → 「已过期」（仅前置提示）；
 *  - 默认值用 placeholder 表达，摘要全空 → 「默认」。
 *
 *  换算唯一执行点在 `amend.resolveDeadlineDraft`（提交时刻 + 预览时刻两处消费同一
 *  实现）——这里对它做确定性注入 now 的直接测试；`toCreateBudget` 的 wire 语义由
 *  amend.test.ts 锁定。 */

import { describe, expect, it } from 'vitest';
import { parseDurationToken, resolveDeadlineDraft } from './amend';
import {
  DURATION_CUSTOM_ID,
  DURATION_TIERS,
  TOKEN_CUSTOM_ID,
  TOKEN_TIERS,
  budgetSummary,
  deadlinePreview,
  durationTierId,
  tokensLabel,
  tokensTierId,
} from './budgetUi';

// ── 档位目录（票面 §2.1：100k / 250k / 500k / 1M / 自定义）──

describe('预算档位目录（#536 §2.1）', () => {
  it('tokens 档位恰好是 100k / 250k / 500k / 1M 四档（外加自定义哨兵）', () => {
    expect(TOKEN_TIERS.map((t) => t.value)).toEqual([100_000, 250_000, 500_000, 1_000_000]);
    expect(TOKEN_TIERS.map((t) => t.label)).toEqual(['100k', '250k', '500k', '1M']);
    expect(TOKEN_CUSTOM_ID).not.toBe(TOKEN_TIERS[0].id);
  });

  it('时长档恰好是 30m / 1h / 2h / 4h（外加自定义时长哨兵）', () => {
    expect(DURATION_TIERS.map((t) => t.token)).toEqual(['30m', '1h', '2h', '4h']);
    expect(DURATION_CUSTOM_ID).not.toBe(DURATION_TIERS[0].id);
  });
});

// ── 时长 token 解析（自定义时长的唯一判形点；与 turns/tokens 同语义：非法视同未设）──

describe('parseDurationToken — 时长档判形（#536 §2.2）', () => {
  it('合法形态 → 分钟数（"30m"→30、"1h"→60、"2h"→120、"90m"→90）', () => {
    expect(parseDurationToken('30m')).toBe(30);
    expect(parseDurationToken('1h')).toBe(60);
    expect(parseDurationToken('2h')).toBe(120);
    expect(parseDurationToken('90m')).toBe(90);
  });

  it('0 与负数不是合法时长（ge=1 语义同 turns/tokens）→ null', () => {
    expect(parseDurationToken('0m')).toBeNull();
    expect(parseDurationToken('0h')).toBeNull();
  });

  it('datetime-local 原始值 / 半截 / 非法 → null（走高级路径，不是时长）', () => {
    expect(parseDurationToken('2026-10-02T15:30')).toBeNull();
    expect(parseDurationToken('2')).toBeNull();
    expect(parseDurationToken('h')).toBeNull();
    expect(parseDurationToken('2x')).toBeNull();
    expect(parseDurationToken('')).toBeNull();
  });
});

// ── 档位 id 反查（OptionPicker 的 value 需要）──

describe('tokensTierId / durationTierId — 草稿 → 档位 id', () => {
  it('draft 精确命中档位 → 档位 id', () => {
    expect(tokensTierId('500000')).toBe(TOKEN_TIERS[2].id);
    expect(tokensTierId('100000')).toBe(TOKEN_TIERS[0].id);
    expect(durationTierId('2h')).toBe(DURATION_TIERS[2].id);
  });

  it('非档位的合法值 → 自定义哨兵（300k / 90m）', () => {
    expect(tokensTierId('300000')).toBe(TOKEN_CUSTOM_ID);
    expect(durationTierId('90m')).toBe(DURATION_CUSTOM_ID);
  });

  it('空 / 非法 / datetime-local → null（档位显示"未选"）', () => {
    expect(tokensTierId('')).toBeNull();
    expect(tokensTierId('abc')).toBeNull();
    expect(durationTierId('')).toBeNull();
    expect(durationTierId('2026-10-02T15:30')).toBeNull();
  });
});

// ── tokens 紧凑标签（trigger 摘要用；档位标签 + 自定义 k/M 格式化）──

describe('tokensLabel — 草稿 → 紧凑标签（#536 §2.5 摘要）', () => {
  it('档位值 → 档位标签', () => {
    expect(tokensLabel('100000')).toBe('100k');
    expect(tokensLabel('1000000')).toBe('1M');
  });

  it('自定义值 → k / M 紧凑格式（一位小数去尾零）', () => {
    expect(tokensLabel('300000')).toBe('300k');
    expect(tokensLabel('1500000')).toBe('1.5M');
    expect(tokensLabel('2000000')).toBe('2M');
    expect(tokensLabel('750')).toBe('750');
  });

  it('空 / 非法 → null', () => {
    expect(tokensLabel('')).toBeNull();
    expect(tokensLabel('-5')).toBeNull();
    expect(tokensLabel('abc')).toBeNull();
  });
});

// ── 摘要（trigger 常显文本；§2.5：默认值用 placeholder 表达）──

describe('budgetSummary — trigger 摘要（#536 §2.5）', () => {
  it('全空 → 「默认」', () => {
    expect(budgetSummary('', '', '')).toBe('默认');
  });

  it('部分设置 → turns 恒在（默认/数值），tokens 与 deadline 设了才出', () => {
    // 设计稿示例形态：turns 未设 + 500k + 2h。
    expect(budgetSummary('', '500000', '2h')).toBe('turns 默认 · 500k · 2h');
    expect(budgetSummary('5', '', '')).toBe('turns 5');
    expect(budgetSummary('5', '250000', '')).toBe('turns 5 · 250k');
    expect(budgetSummary('', '', '30m')).toBe('turns 默认 · 30m');
  });

  it('datetime 草稿 → 摘要里显示 MM-DD HH:mm（不是原始 datetime-local 串）', () => {
    const summary = budgetSummary('', '', '2026-10-02T15:30');
    expect(summary).toBe('turns 默认 · 10-02 15:30');
  });

  it('非数字 turns / 非法 tokens → 该部分按未设处理（判形唯一执行点一致）', () => {
    expect(budgetSummary('abc', 'xyz', '')).toBe('默认');
  });
});

// ── 预览行（§2.2：「≈ 10-02 15:30 截止（2 小时后）」；过期 → 「已过期」）──

describe('deadlinePreview — 截止预览（#536 §2.2）', () => {
  // 固定 now：本地 2026-10-02 13:30（ISO 即 UTC 视测试环境 TZ 而定，格式断言只看
  // 时分文本与相对语，跨时区稳定）。
  const NOW = new Date('2026-10-02T13:30:00');

  it('空草稿 → null（不渲染预览行）', () => {
    expect(deadlinePreview('', NOW)).toBeNull();
  });

  it('时长档 → 绝对时刻 + 相对语（"2h" → （2 小时后））', () => {
    const preview = deadlinePreview('2h', NOW);
    expect(preview).not.toBeNull();
    expect(preview?.expired).toBe(false);
    // 绝对时刻必须等于 now + 2h（换算唯一执行点 resolveDeadlineDraft）。
    const expected = resolveDeadlineDraft('2h', NOW);
    expect(preview?.text).toContain('截止');
    expect(preview?.text).toContain('（2 小时后）');
    expect(expected).toBeDefined();
  });

  it('分钟档 → （30 分钟后）', () => {
    expect(deadlinePreview('30m', NOW)?.text).toContain('（30 分钟后）');
  });

  it('datetime-local 高级路径 → 同一格式（含日期与时刻），无相对语', () => {
    const preview = deadlinePreview('2026-10-02T15:30', NOW);
    expect(preview?.expired).toBe(false);
    expect(preview?.text).toMatch(/≈ \d{2}-\d{2} \d{2}:\d{2} 截止$/);
  });

  it('跨年 → 显示完整年份（YYYY-MM-DD HH:mm）', () => {
    const preview = deadlinePreview('2027-01-05T09:00', NOW);
    expect(preview?.text).toMatch(/≈ 2027-01-05 \d{2}:\d{2} 截止/);
  });

  it('已过期（datetime 时刻早于 now）→ 「已过期」 + expired=true（渲染层配 --danger）', () => {
    const preview = deadlinePreview('2026-10-02T09:00', NOW);
    expect(preview?.expired).toBe(true);
    expect(preview?.text).toBe('已过期');
  });

  it('时长档 resolve 后必然不早于 now → 永不过期', () => {
    const preview = deadlinePreview('30m', NOW);
    expect(preview?.expired).toBe(false);
  });

  it('非法草稿 → null（不渲染预览行）', () => {
    expect(deadlinePreview('not-a-date', NOW)).toBeNull();
  });
});

// ── resolveDeadlineDraft（amend 导出的换算唯一执行点；确定性注入 now）──

describe('resolveDeadlineDraft — 时长 / datetime 双形态换算（#536）', () => {
  const NOW = new Date('2026-10-02T13:30:00');

  it('时长 token → now + 时长的 RFC 3339 UTC 瞬时', () => {
    expect(resolveDeadlineDraft('2h', NOW)).toBe(new Date(NOW.getTime() + 2 * 3600_000).toISOString());
    expect(resolveDeadlineDraft('30m', NOW)).toBe(new Date(NOW.getTime() + 30 * 60_000).toISOString());
    expect(resolveDeadlineDraft('90m', NOW)).toBe(new Date(NOW.getTime() + 90 * 60_000).toISOString());
  });

  it('datetime-local 原路径不变（既有 wire 语义逐字节一致）', () => {
    expect(resolveDeadlineDraft('2026-10-01T12:30', NOW)).toBe(new Date('2026-10-01T12:30').toISOString());
  });

  it('空 / 0 时长 / 非法 → undefined（不发键语义与既有判形一致）', () => {
    expect(resolveDeadlineDraft(null, NOW)).toBeUndefined();
    expect(resolveDeadlineDraft('', NOW)).toBeUndefined();
    expect(resolveDeadlineDraft('0m', NOW)).toBeUndefined();
    expect(resolveDeadlineDraft('not-a-date', NOW)).toBeUndefined();
  });
});
