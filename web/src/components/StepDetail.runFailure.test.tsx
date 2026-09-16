/** #220 失败归因的渲染契约（机制全文见 ADR-0033）。
 *  失败当下用户必须能看见后端已经算出来的原因——此前 Inspector 只显示「失败」两字，
 *  真因要翻服务端日志。这里锁三件事：
 *   - 有文案 → 「失败原因」行渲染文案（并把分类码缀在后面）；
 *   - 只有码（工具失败保险丝）→ 用码兜底，仍是「失败原因」行；
 *   - 都没有 / 取消 → **不渲染这一行**（不铺空槽、不编文案）。
 *  与 `StepDetail.trace.test.tsx` 同构：ChatTab 的 SSR 渲染断言，不起浏览器。 */

import { describe, expect, it } from 'vitest';
import { createElement } from 'react';
import { renderToString } from 'react-dom/server';
import { EventType } from '../types';
import type { AgentEvent, ConversationState } from '../types';
import { initConversation, applyEvent } from '../lib/projection';
import { ChatTab } from './StepDetail';

const strip = (html: string) => html.replaceAll('<!-- -->', '');

const render = (conv: ConversationState) =>
  strip(renderToString(createElement(ChatTab, { conversation: conv, tools: [] })));

/** 「失败原因」那一行的**值**（`.run-failure-val` 的文本）。
 *  为什么必须取到值域而不是对整个 HTML 用 `toContain(码)`：#222 真机实测发现
 *  `toContain('identical_tool_failure_loop')` 在**值域为空**的树上照样绿——那个码
 *  还出现在**同一元素**的 `title` 兜底属性里（`StepDetail.tsx` 的 `title={message
 *  ?? reason ?? ''}`，与值域一同改的），于是"字符串在整页里存在"被当成了"这一行
 *  显示了它"。`rowValue` 只取值域，`title` 命中不了它。 */
const rowValue = (html: string) => {
  const m = html.match(/class="detail-val run-failure-val"[^>]*>([\s\S]*?)<\/span>/);
  return m ? m[1].replaceAll(/<[^>]+>/g, '').trim() : null;
};

/** run → 失败收尾的最小日志（投影走真实 applyEvent，不手搓 ConversationState）。 */
function convFailedWith(data: Record<string, unknown>): ConversationState {
  let s = initConversation('f');
  for (const ev of [
    { type: EventType.RUN_STARTED, data: { turn_index: 1 }, seq: 1, run_id: 'r1', session_id: 'f' },
    { type: EventType.RUN_FAILED, data, seq: 2, run_id: 'r1', session_id: 'f' },
  ] as AgentEvent[]) s = applyEvent(s, ev);
  return s;
}

describe('StepDetail 失败原因行（#220 / ADR-0033）', () => {
  it('已分类失败 → 文案与分类码都渲染（不再是只有「失败」两字）', () => {
    const html = render(convFailedWith({
      reason: 'provider_account_unavailable',
      message: '模型供应商账户不可用（欠费 / 配额耗尽 / 账户被冻结），请到供应商控制台检查计费与配额',
    }));
    expect(html).toContain('失败原因');
    expect(html).toContain('模型供应商账户不可用'); // 可操作文案本体
    expect(html).toContain('请到供应商控制台检查计费与配额'); // 处置建议的尾巴（#220 的重点）
    expect(html).toContain('provider_account_unavailable'); // 机器可读分类码
    // 长文案必须允许换行：默认 Inspector（340px）下 nowrap 会把它切成省略号
    expect(html).toContain('run-failure-val');
    // 值域 = 文案 + 码（两个都在这一行里）
    expect(rowValue(html)).toBe(
      '模型供应商账户不可用（欠费 / 配额耗尽 / 账户被冻结），请到供应商控制台检查计费与配额 provider_account_unavailable',
    );
  });

  it('只有分类码（工具失败保险丝）→ 这一行的**值**就是码（不是空值）', () => {
    const html = render(convFailedWith({ reason: 'identical_tool_failure_loop' }));
    expect(html).toContain('失败原因');
    expect(rowValue(html)).toBe('identical_tool_failure_loop');
    // 假绿溯源（可执行的版本）：码在整页出现两次——`title` 属性 + 值域。旧断言
    // 用的 `toContain` 靠前者命中，所以它在"值域为空"的树上也是绿的。
    expect([...html.matchAll(/identical_tool_failure_loop/g)]).toHaveLength(2);
  });

  it('#222 未分类失败（后端给类型名当码、不给文案）→ 值域仍是码，不能是空行', () => {
    // 真机形状：MODEL_BASE_URL 指向死端口 ⇒ run/failed {"reason":"RateLimitError"}。
    // 修复前这一行渲染出「失败原因」标签 + **空值**（码只在 message 同时存在时才缀出来）。
    const html = render(convFailedWith({ reason: 'RateLimitError' }));
    expect(rowValue(html)).toBe('RateLimitError');
  });

  it('未分类失败（两键都不落）→ 不渲染这一行（没有后端文案时不铺空槽）', () => {
    expect(render(convFailedWith({}))).not.toContain('失败原因');
    expect(rowValue(render(convFailedWith({})))).toBeNull();
  });

  it('取消（即便后端给了文案）→ 不渲染这一行（取消 ≠ 失败）', () => {
    const cancelled = render(convFailedWith({ reason: 'cancelled', message: '用户取消' }));
    expect(cancelled).not.toContain('失败原因');
    expect(cancelled).not.toContain('用户取消');
  });

  it('未曾失败（空会话 / 跑成功）→ 不渲染这一行', () => {
    expect(render(initConversation('f'))).not.toContain('失败原因');
    const completed = applyEvent(
      convFailedWith({ reason: 'provider_auth_failed', message: '…鉴权失败…' }),
      { type: EventType.RUN_COMPLETED, data: {}, seq: 3, run_id: 'r1', session_id: 'f' } as AgentEvent,
    );
    expect(render(completed)).not.toContain('失败原因');
  });
});
