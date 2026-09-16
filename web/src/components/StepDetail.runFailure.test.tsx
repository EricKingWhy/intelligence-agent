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
  });

  it('只有分类码（工具失败保险丝）→ 仍渲染这一行，用码兜底', () => {
    const html = render(convFailedWith({ reason: 'identical_tool_failure_loop' }));
    expect(html).toContain('失败原因');
    expect(html).toContain('identical_tool_failure_loop');
  });

  it('未分类失败（两键都不落）→ 不渲染这一行（没有后端文案时不铺空槽）', () => {
    expect(render(convFailedWith({}))).not.toContain('失败原因');
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
