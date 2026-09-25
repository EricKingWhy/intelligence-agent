/** PausedPanel 渲染（`#312` T4）。
 *
 *  静态渲染（renderToStaticMarkup，同 ApprovalCard.test.tsx 的口径）：锁的是
 *  「暂停事实与恢复入口长什么样」，以及票面明写的三条显示要求——
 *  ①不只说"暂停了"：reason / consumed / limit / version / continuation 全在场；
 *  ②与失败/完成分开措辞（不出现「失败」字样）；
 *  ③缺数字时写 unlimited / unavailable，**不写 0**（`11 §6.1` 零伪造）。
 *
 *  交互（提交恢复请求、409 后重读日志）由 useSession/api 层负责，不在本文件。 */

import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { PausedPanel } from './PausedPanel';
import type { RunPausedInfo } from '../types';

function paused(overrides: Partial<RunPausedInfo> = {}): RunPausedInfo {
  return {
    run_id: 'run-1',
    version: 2,
    pause_seq: 9,
    reason: 'budget_exhausted',
    trigger_dimension: 'run.max_agent_turns_total',
    consumed_agent_turns: 3,
    run_limit: 8,
    // `#313` 四维组：本夹具是**老暂停载荷**形状（事件没带 `consumed` / `limits.run`）⇒ null，
    // 语义是"该维读数未知"而不是 0。四维展示的用例各自显式传值。
    consumed_dimensions: null,
    run_limits: null,
    local_fuse: { max_agent_turns: 500, source: 'deployment' },
    continuation: {
      completed: ['本逻辑 run 已消耗 3 个 agent turn'],
      remaining: ['暂停发生在下一轮模型决策之前'],
      blockers: ['run.max_agent_turns_total 到顶：consumed=3, ceiling=8'],
      next_safe_action: '提高绝对 ceiling 后以同一 run_id 恢复',
    },
    closeout_source: 'model',
    resume_requirements: [],
    trace_id: 'trace-xyz',
    ...overrides,
  };
}

function render(p: RunPausedInfo, options: { draft?: string; resuming?: boolean } = {}): string {
  return renderToStaticMarkup(
    <PausedPanel
      paused={p}
      ceilingDraft={options.draft ?? '10'}
      onCeilingDraftChange={() => {}}
      onResume={() => {}}
      resuming={options.resuming ?? false}
    />,
  );
}

describe('PausedPanel — 暂停事实的完整呈现（#312）', () => {
  it('reason / consumed / limit / version / closeout / continuation 全部在场', () => {
    const html = render(paused());
    expect(html).toContain('已在预算到顶处暂停');
    expect(html).toContain('run.max_agent_turns_total');
    expect(html).toContain('budget_exhausted');
    // 读数按**命中的那一维**报（`#313`）：turns 命中时标题给出本轮读数，四维清单
    // 再把它逐维列出（与 CLI 的行文同一份事实，措辞按 Web 的排版）。
    expect(html).toContain('run 累计轮次到顶（run.max_agent_turns_total）：已消耗 3');
    expect(html).toContain('绝对 ceiling 8');
    expect(html).toContain('剩余 5');
    expect(html).toContain('agent_turns: 3 / limit 8（剩余 5） · 到顶');
    expect(html).toContain('预算版本 2');
    expect(html).toContain('收口 model');
    expect(html).toContain('local fuse 500（deployment）');
    expect(html).toContain('trace-xyz');
    expect(html).toContain('提高绝对 ceiling 后以同一 run_id 恢复');
    expect(html).toContain('本逻辑 run 已消耗 3 个 agent turn');
  });

  it('与失败/完成分开措辞：状态行只说暂停，不说完成/失败', () => {
    const html = render(paused());
    // 只对**状态行**下断言：continuation 段里出现「已完成：」是合同规定的字段标签
    // （它说的是"这一段计划里已完成的部分"），拿整段 HTML 做子串否定会把它误判成
    // "面板声称 run 已完成"。
    const head = html.slice(0, html.indexOf('pause-panel-facts'));
    expect(head).toContain('已在预算到顶处暂停');
    expect(head).toContain('不是失败');
    expect(head).not.toContain('已完成');
    expect(head).not.toContain('失败：');
  });

  it('暂停落在 requests 维：标题与恢复输入都指 requests（不是"消耗 0 轮 · 无上限"）', () => {
    const html = render(
      paused({
        trigger_dimension: 'run.max_model_requests',
        consumed_agent_turns: 3,
        run_limit: 8,
        consumed_dimensions: { agent_turns: 3, model_requests: 4, total_tokens: 120, cost_usd: null },
        run_limits: {
          max_agent_turns_total: 8,
          max_model_requests: 4,
          max_total_tokens: null,
          max_cost_usd: null,
        },
      }),
    );
    expect(html).toContain('run 累计模型请求到顶（run.max_model_requests）：已消耗 4');
    expect(html).toContain('model_requests: 4 / limit 4（剩余 0） · 到顶');
    // turns 维没到顶也要在清单里（事实完整），但它不是"卡住的那一维"。
    expect(html).toContain('agent_turns: 3 / limit 8（剩余 5）');
    expect(html).not.toContain('agent_turns: 3 / limit 8（剩余 5） · 到顶');
    // 恢复输入抬的是 requests，且预校验按 requests 的消耗算（4 + 预留 1 + 1 = 6）。
    expect(html).toContain('绝对 ceiling（max_model_requests）');
    expect(html).toContain('至少 6');
    expect(html).toContain('抬的是 max_model_requests');
    // cost 维两边都没事实 ⇒ 不渲染（多打一行 unavailable/unlimited 是噪声不是信息）。
    expect(html).not.toContain('cost_usd');
  });

  it('暂停落在 cost 维：十进制读数按字符串原样呈现，恢复输入按十进制校验', () => {
    const html = render(
      paused({
        trigger_dimension: 'run.max_cost_usd',
        consumed_dimensions: {
          agent_turns: 3, model_requests: 4, total_tokens: 120, cost_usd: '1.25',
        },
        run_limits: {
          max_agent_turns_total: null,
          max_model_requests: null,
          max_total_tokens: null,
          max_cost_usd: '1.25',
        },
      }),
      { draft: '1.50' },
    );
    expect(html).toContain('cost_usd: 1.25 / limit 1.25（剩余 0.00） · 到顶');
    expect(html).toContain('绝对 ceiling（max_cost_usd）');
    // 计量维度不能说"至少 N"（判据是严格大于已消耗，连续域上没有最小值）——
    // 提示必须如实说规则，2.25 只是默认值。
    expect(html).toContain('须严格大于已消耗 1.25');
    expect(html).toContain('value="1.50"');
  });

  it('未配 run ceiling：写 unlimited 与"未声明"（不写 0 冒充）', () => {
    const html = render(paused({ run_limit: null, local_fuse: null, continuation: null }));
    expect(html).toContain('unlimited');
    expect(html).not.toContain('剩余 0');
    expect(html).not.toContain('local fuse');
    expect(html).not.toContain('下一步：');
  });

  it('恢复入口：草稿非法时禁用按钮并就地说明原因', () => {
    const html = render(paused(), { draft: '3' });
    expect(html).toMatch(/<button[^>]*disabled[^>]*>/);
    expect(html).toContain('至少 5');
  });

  it('草稿合法时按钮可点，并显示最小合法值与"消耗不重置"的提示', () => {
    const html = render(paused(), { draft: '10' });
    expect(html).not.toMatch(/<button[^>]*disabled/);
    expect(html).toContain('至少 5');
    expect(html).toContain('已消耗不重置');
  });

  it('提交在途：按钮禁用 + 文案改为恢复中（防两次 CAS 请求）', () => {
    const html = render(paused(), { draft: '10', resuming: true });
    expect(html).toContain('恢复中…');
    expect(html).not.toMatch(/<button[^>]*disabled[^>]*>\s*<svg[^>]*><\/svg>恢复同一 run/);
  });

  it('有恢复前置条件时如实列出（本票预算暂停为空数组，故不渲染空段）', () => {
    expect(render(paused())).not.toContain('恢复前置条件');
    expect(render(paused({ resume_requirements: ['stuck 证据'], continuation: null }))).toContain(
      '恢复前置条件：stuck 证据',
    );
  });

  it('输入框是受控的：草稿原样回显（用户输入不丢）', () => {
    expect(render(paused(), { draft: '42' })).toContain('value="42"');
  });
});
