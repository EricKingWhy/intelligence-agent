/** `usageOnlyNote` 的措辞锁（#212 审查项）。
 *
 * 为什么单独测这个纯函数：这句说明宣称的是"总数取自哪里"，而它必须**跟着后端给的
 * `usage_source.kind` 走**。绑 `state` 写死的话，后端换取值口径时界面会继续宣称一个
 * 假的来源，而 tsc / vitest / e2e 全绿（e2e 夹具是写死的）——正是本仓当 P1 处理的那类
 * "UI 说后端做不到/不是那个意思的事"。 */

import { describe, expect, it } from 'vitest';
import { usageOnlyNote } from '../lib/contextUsageNote';
import type { ContextUsage } from '../lib/api';

const usage = (source?: ContextUsage['usage_source']): ContextUsage => ({
  estimated: true,
  window_tokens: 200_000,
  used_tokens: 54_841,
  thresholds: { auto_compact: 0.7, hard_guard: 0.85 },
  breakdown: { messages: 0, system_prompt: 0, skills: 0, other: 0, tools: { system: 0, mcp: 0 } },
  cache: { state: 'partial', reported_calls: 1, total_calls: 2, avg_hit_rate: 0.9118 },
  state: 'usage_only',
  ...(source ? { usage_source: source } : {}),
});

const SOURCE: NonNullable<ContextUsage['usage_source']> = {
  kind: 'last_call_prompt_tokens',
  calls_with_usage: 2,
  last_prompt_tokens: 54_841,
  last_total_tokens: 61_342,
};

describe('usageOnlyNote', () => {
  it('已知 kind（prompt_tokens）→ 逐字说明"输入规模（窗口占用下界）" + 调用次数', () => {
    expect(usageOnlyNote(usage(SOURCE))).toBe(
      '分类未采集：总数取自最近一次调用的输入规模（窗口占用下界），本会话 2 次调用有用量上报',
    );
  });

  it('未知 kind → 中性说法（**不**继续宣称"输入规模"）', () => {
    // 后端将来翻案改用 total_tokens（设计稿 §3.4：只改一行取值、不动契约形状）时，
    // 这句必须跟着变——且未知码不许解释成已知的那一种。
    const note = usageOnlyNote(usage({ ...SOURCE, kind: 'last_call_total_tokens' }));
    expect(note).toBe('分类未采集：总数取自最近一次调用的用量上报，本会话 2 次调用有用量上报');
    expect(note).not.toContain('输入规模');
  });

  it('后端没给 usage_source → 只说"为什么"，不编调用次数', () => {
    expect(usageOnlyNote(usage())).toBe('分类未采集：总数取自最近一次调用的用量上报');
  });

  it('任何一支都不出现"后端未上报用量数据"（那是 no_data 的话）', () => {
    for (const s of [SOURCE, { ...SOURCE, kind: 'other' }, undefined]) {
      expect(usageOnlyNote(usage(s))).not.toContain('未上报');
    }
  });
});
