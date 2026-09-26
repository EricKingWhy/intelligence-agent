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
        consumed_dimensions: {
          agent_turns: 3, model_requests: 4, total_tokens: 120, cost_usd: null,
          tool_calls: 0, tool_attempts: 0, tool_calls_by_tool: {}, tool_attempts_by_tool: {},
        },
        run_limits: {
          max_agent_turns_total: 8,
          max_model_requests: 4,
          max_total_tokens: null,
          max_cost_usd: null,
          tool_call_limits: {},
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
          tool_calls: 0, tool_attempts: 0, tool_calls_by_tool: {}, tool_attempts_by_tool: {},
        },
        run_limits: {
          max_agent_turns_total: null,
          max_model_requests: null,
          max_total_tokens: null,
          max_cost_usd: '1.25',
          tool_call_limits: {},
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

  it('暂停落在 per-tool 配额（`#314`）：两个 counter 分开显示，恢复输入点名那个工具', () => {
    const html = render(
      paused({
        trigger_dimension: 'run.tool_call_limits.glob',
        consumed_agent_turns: 2,
        run_limit: 8,
        consumed_dimensions: {
          agent_turns: 2, model_requests: 3, total_tokens: 40, cost_usd: null,
          // 一次逻辑调用、三次真实尝试：retry 不是新的逻辑调用（`02 §5.1`）。
          tool_calls: 1, tool_attempts: 3,
          tool_calls_by_tool: { glob: 1 }, tool_attempts_by_tool: { glob: 3 },
        },
        run_limits: {
          max_agent_turns_total: 8,
          max_model_requests: null,
          max_total_tokens: null,
          max_cost_usd: null,
          tool_call_limits: { glob: 1, bash: 5 },
        },
      }),
      { draft: '2' },
    );
    // 标题报的是**那个工具**的读数（不是 turns 的 2/8）。
    expect(html).toContain('工具 glob 的本 run 调用配额到顶（run.tool_call_limits.glob）');
    expect(html).toContain('已消耗 1 次调用（3 次尝试）');
    expect(html).toContain('绝对 ceiling 1');
    expect(html).toContain('剩余 0');
    // 清单逐工具一行，两个 counter 分开写；配了没调过的工具也在——表已知时读数就是
    // **0 / 剩余 = ceiling**（后端 `BudgetConsumed.calls_for` 的口径：表在 ⇒ 缺名 = 0）。
    expect(html).toContain('tool glob: consumed 1 calls / 3 attempts / limit 1（剩余 0） · 到顶');
    expect(html).toContain('tool bash: consumed 0 calls / 0 attempts / limit 5（剩余 5）');
    // 恢复输入抬的是工具配额（键路径 + 真实 argv 形状的 CLI 开关）。
    expect(html).toContain('绝对 ceiling（tool_call_limits.glob）');
    expect(html).toContain('抬的是 tool_call_limits.glob');
    expect(html).toContain('--run-tool-limit glob=N');
    // turns 维没到顶照旧列着（事实完整），但它不是卡住的那一维。
    expect(html).toContain('agent_turns: 2 / limit 8（剩余 6）');
  });

  it('命中 run 维时也照旧列出工具配额清单（它是事实，不是"卡住的那一维"）', () => {
    const html = render(
      paused({
        consumed_dimensions: {
          agent_turns: 3, model_requests: 4, total_tokens: 120, cost_usd: null,
          tool_calls: 2, tool_attempts: 2,
          tool_calls_by_tool: { glob: 2 }, tool_attempts_by_tool: { glob: 2 },
        },
        run_limits: {
          max_agent_turns_total: 8,
          max_model_requests: null,
          max_total_tokens: null,
          max_cost_usd: null,
          tool_call_limits: {},
        },
      }),
      { draft: '10' },
    );
    expect(html).toContain('tool glob: consumed 2 calls / 2 attempts / limit unlimited（剩余 unavailable）');
    expect(html).not.toContain(' · 到顶（run.tool_call_limits');
    // 恢复输入照旧抬 turns（工具配额只是清单里的事实）。
    expect(html).toContain('绝对 ceiling（max_agent_turns_total）');
  });

  it('没有任何工具配额事实时不渲染这一节（老暂停：多打一片 unavailable 是噪声）', () => {
    const html = render(paused());
    expect(html).not.toContain('tool ');
    expect(html).not.toContain('tool_call_limits');
  });
});


describe('PausedPanel — deadline 暂停（`#315` T7）', () => {
  function deadlinePaused(overrides: Partial<RunPausedInfo> = {}): RunPausedInfo {
    return paused({
      reason: 'deadline',
      trigger_dimension: 'run.deadline_at',
      consumed_dimensions: {
        agent_turns: 2, model_requests: 3, total_tokens: 40, cost_usd: null,
        tool_calls: 1, tool_attempts: 1,
        tool_calls_by_tool: { glob: 1 }, tool_attempts_by_tool: { glob: 1 },
      },
      run_limits: {
        max_agent_turns_total: 8,
        max_model_requests: null,
        max_total_tokens: null,
        max_cost_usd: null,
        deadline_at: '2026-09-26T04:10:00Z',
        tool_call_limits: {},
      },
      ...overrides,
    });
  }

  it('标题报的是**时刻**与到点后的准入边界（不是某一维的读数）', () => {
    const html = render(deadlinePaused());
    expect(html).toContain('已在绝对截止时刻处暂停');
    expect(html).toContain('绝对截止时刻 2026-09-26T04:10:00Z 已到');
    expect(html).toContain('到点后不再接纳新的 Provider');
    expect(html).toContain('run.deadline_at');
    expect(html).toContain('原因 deadline');
    // 不是"预算到顶"那句（两种原因对应不同的恢复动作）。
    expect(html).not.toContain('已在预算到顶处暂停');
  });

  it('恢复输入换成"新的绝对截止时刻"：文本输入 + 时刻示例 + 时刻形状的校验', () => {
    const html = render(deadlinePaused(), { draft: '2099-01-01T00:00:00Z' });
    expect(html).toContain('新的绝对截止时刻');
    expect(html).toContain('RFC 3339 UTC');
    // SSR 输出的是 `inputMode`（HTML 属性名大小写不敏感，浏览器按 `inputmode` 解析；
    // 这条断言锁的是"输入模式换成了文本"，不是某一种大小写写法）。
    expect(html).toContain('inputMode="text"');
    expect(html).toContain('placeholder="2026-09-26T04:30:00Z"');
    expect(html).toContain('换一个');
    // 合法草稿 ⇒ 可提交（按钮不带 disabled 属性）。
    expect(html).not.toContain('pause-resume-btn" disabled');
  });

  it('草稿是已过去的时刻：就地说明原因并禁用按钮（省掉一次必然 409 的往返）', () => {
    const html = render(deadlinePaused(), { draft: '2026-01-01T00:00:00Z' });
    expect(html).toContain('未来');
    expect(html).toContain('disabled');
  });

  it('输入框默认空着（草稿 null）：面板不替用户编一个"现在 + N 分钟"', () => {
    const html = render(deadlinePaused(), { draft: '' });
    expect(html).toContain('请填一个未来时刻');
    expect(html).toContain('disabled');
  });
});
