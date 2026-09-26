/** 预算暂停的脉冲、恢复入口与展示事实（`#312` T4）。
 *
 *  三组断言各自对应票面的一条要求：
 *  - 脉冲：暂停在顶栏/Inspector 与 completed / failed / interrupted **分开**；
 *  - 恢复入口：健康暂停**不**亮「恢复会话」按钮（那是崩溃修复的入口，点了必然 no-op）；
 *  - 展示事实：与 CLI `_pause_facts` 同一口径（缺数字 = unavailable/unlimited，永不写 0）。 */

import { describe, expect, it } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent, RunPausedInfo } from '../types';
import { deriveRunPulse, deriveRunSummary, hasUnterminatedRun, isRecoverableRun } from './runState';
import {
  ceilingDraftError,
  ceilingDraftValue,
  defaultResumeDraft,
  dimensionLabel,
  dimensionRemaining,
  minResumeValue,
  minToolQuotaValue,
  pauseFacts,
  resumeInputHint,
  resumeRequestTarget,
  resumeTargetCliFlag,
  resumeTargetLabel,
  toolQuotaFacts,
  type PauseFacts,
  type RunDimensionSpec,
} from './runBudget';
import { initConversation } from './projection';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: 'run-1', step_id: null, ...partial };
}

function pausedInfo(overrides: Partial<RunPausedInfo> = {}): RunPausedInfo {
  return {
    run_id: 'run-1',
    version: 2,
    pause_seq: 9,
    reason: 'budget_exhausted',
    trigger_dimension: 'run.max_agent_turns_total',
    consumed_agent_turns: 3,
    run_limit: 4,
    // `#313` 四维组：本夹具是**老暂停载荷**形状（事件没带 `consumed` / `limits.run`）⇒ null，
    // 语义是"该维读数未知"而不是 0。需要四维的用例各自显式传值（见下面的 requests 用例）。
    consumed_dimensions: null,
    run_limits: null,
    local_fuse: { max_agent_turns: 500, source: 'deployment' },
    continuation: null,
    closeout_source: 'model',
    resume_requirements: [],
    trace_id: null,
    ...overrides,
  };
}

/** 直接把投影字段摆成暂停态（这些用例测的是**消费端**，不是投影本身——
 *  投影的折叠语义由 projection.pause.test.ts 锁）。 */
function pausedConversation() {
  const state = initConversation('s');
  state.run_status = 'paused';
  state.run_paused = pausedInfo();
  return state;
}

/** 命中的恢复目标是 **run 维**时的 spec（run 维用例的窄化辅助）。窄化失败直接抛：
 *  静默读到 `undefined` 会让断言变成"测一个不存在的字段"。 */
function targetRunSpec(facts: PauseFacts): RunDimensionSpec {
  if (facts.resumeTarget.kind !== 'run') {
    throw new Error(`期望 run 维的恢复目标，实际是 ${facts.resumeTarget.kind}`);
  }
  return facts.resumeTarget.spec;
}

describe('deriveRunPulse — 暂停是独立一档（#312）', () => {
  it('run_status=paused ⇒ 脉冲「已暂停」+ pulse-paused，不是已完成/失败', () => {
    const pulse = deriveRunPulse(pausedConversation(), false);
    expect(pulse.state).toBe('paused');
    expect(pulse.label).toBe('已暂停');
    expect(pulse.className).toBe('pulse-paused');
  });

  it('附着在还挂着的流上时仍是「已暂停」（不因 streaming 被读成「思考中」）', () => {
    expect(deriveRunPulse(pausedConversation(), true).state).toBe('paused');
  });

  it('Inspector 粗口径同档：不允许被粗化成「运行中」或「已完成」', () => {
    expect(deriveRunSummary(pausedConversation()).label).toBe('已暂停');
  });

  it('暂停与「已中断」是两档（不能互相冒充）', () => {
    const interrupted = initConversation('s');
    interrupted.run_status = 'completed';
    interrupted.run_interrupted = { step_id: 1, interrupted_seq: 5, reason: 'process_restart' };
    expect(deriveRunPulse(interrupted, false).state).toBe('interrupted');
    expect(deriveRunPulse(pausedConversation(), false).state).toBe('paused');
  });
});

describe('scanRuns —— 暂停算收口、恢复再打开（与后端 interrupt.py 对齐）', () => {
  it('run/paused 之后不再算"缺 run 终态"：健康暂停不亮「恢复会话」', () => {
    const events = [
      ev({ type: EventType.RUN_STARTED }),
      ev({ type: EventType.MODEL_COMPLETED }),
      ev({ type: EventType.RUN_PAUSED }),
    ];
    expect(hasUnterminatedRun(events)).toBe(false);
    expect(isRecoverableRun(events)).toBe(false);
  });

  it('run/resumed 把同一个 run 重新打开：暂停期间"已收口"的判断随之作废', () => {
    const events = [
      ev({ type: EventType.RUN_STARTED }),
      ev({ type: EventType.RUN_PAUSED }),
      ev({ type: EventType.RUN_RESUMED }),
    ];
    expect(hasUnterminatedRun(events)).toBe(true);
  });

  it('暂停的会话仍有 dangling 工具调用 → 恢复入口照旧可见（两个修复面互不掩盖）', () => {
    const events = [
      ev({ type: EventType.RUN_STARTED }),
      ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1' } }),
      ev({ type: EventType.RUN_PAUSED }),
    ];
    expect(isRecoverableRun(events)).toBe(true);
  });
});

describe('pauseFacts —— 与 CLI 同一口径的展示数字（#312）', () => {
  it('consumed / ceiling / remaining / local fuse / 版本逐项对应事件真值', () => {
    const facts = pauseFacts(pausedInfo());
    expect(facts.consumed).toBe(3);
    expect(facts.ceiling).toBe(4);
    expect(facts.remaining).toBe(1);
    expect(facts.localFuseTurns).toBe(500);
    expect(facts.localFuseSource).toBe('deployment');
    expect(facts.version).toBe(2);
    // 后端判据 ceiling > consumed + RESERVED_CLOSEOUT_TURNS ⇒ 最小合法值 = consumed + 2
    expect(facts.minResumeCeiling).toBe(5);
  });

  it('未配 run ceiling：ceiling/remaining 都是 null（渲染成 unlimited，不是 0）', () => {
    const facts = pauseFacts(pausedInfo({ run_limit: null, local_fuse: null }));
    expect(facts.ceiling).toBeNull();
    expect(facts.remaining).toBeNull();
    expect(facts.localFuseTurns).toBeNull();
  });

  it('四个 run 维度与 local fuse 各有标签；未知维度原样回显（不编名字）', () => {
    expect(dimensionLabel('run.max_agent_turns_total')).toContain('run 累计轮次到顶');
    expect(dimensionLabel('local.max_agent_turns')).toContain('单次执行保险丝到顶');
    // `#313` 起这四个维度都可能命中：它们各自有标签，不打裸串（review P2-3）。
    expect(dimensionLabel('run.max_model_requests')).toContain('run 累计模型请求到顶');
    expect(dimensionLabel('run.max_total_tokens')).toContain('run 累计 token 到顶');
    expect(dimensionLabel('run.max_cost_usd')).toContain('run 累计成本到顶');
    expect(dimensionLabel('run.unknown_dimension')).toBe('run.unknown_dimension');
    expect(dimensionLabel('')).toBe('预算维度未声明');
  });

  it('暂停落在 requests / tokens / cost：读数与剩余按**那一维**算（不是 turns 的）', () => {
    const requests = pauseFacts(
      pausedInfo({
        trigger_dimension: 'run.max_model_requests',
        consumed_dimensions: {
          agent_turns: 3, model_requests: 4, total_tokens: 120, cost_usd: null,
          tool_calls: 0, tool_attempts: 0, tool_calls_by_tool: {}, tool_attempts_by_tool: {},
        },
        run_limits: {
          max_agent_turns_total: 8, max_model_requests: 4,
          max_total_tokens: null, max_cost_usd: null, tool_call_limits: {},
        },
      }),
    );
    expect(requests.tripped?.spec.dimension).toBe('run.max_model_requests');
    expect(requests.tripped?.consumedText).toBe('4');
    expect(requests.tripped?.remainingText).toBe('0');
    expect(targetRunSpec(requests).resumeField).toBe('max_model_requests');
    // 只列有事实的维度：turns / requests 有消耗与 ceiling，tokens 只有消耗快照
    // （照样要列——"花了 120 token 但没配上限"本身是事实），cost 两边都没有 ⇒ 不列。
    expect(requests.dimensions.map((fact) => fact.spec.consumedKey)).toEqual([
      'agent_turns', 'model_requests', 'total_tokens',
    ]);
    const tokens = requests.dimensions[2];
    expect(tokens.ceilingText).toBe('unlimited');
    expect(tokens.remainingText).toBe('unavailable');
    // requests 维的恢复阈值含 closeout 预留：4 + 1 + 1 = 6。
    expect(minResumeValue(targetRunSpec(requests), requests.tripped!)).toBe(6);
  });

  it('cost 维：十进制字符串读数与剩余**精确**相减（不经过 float）', () => {
    const cost = pauseFacts(
      pausedInfo({
        trigger_dimension: 'run.max_cost_usd',
        consumed_dimensions: {
          agent_turns: 3, model_requests: 4, total_tokens: 120, cost_usd: '0.10',
          tool_calls: 0, tool_attempts: 0, tool_calls_by_tool: {}, tool_attempts_by_tool: {},
        },
        run_limits: {
          max_agent_turns_total: null, max_model_requests: null,
          max_total_tokens: null, max_cost_usd: '0.30', tool_call_limits: {},
        },
      }),
    );
    // 0.30 - 0.10 在浮点里是 0.19999999999999998；这里必须逐字是 0.20。
    expect(cost.tripped?.remainingText).toBe('0.20');
    expect(cost.tripped?.consumedText).toBe('0.10');
    // 计量维度的建议默认值 = consumed + 1（不是"最小合法值"）。
    expect(minResumeValue(targetRunSpec(cost), cost.tripped!)).toBe('1.10');
    expect(cost.resumeTarget.kind).toBe('run');
    expect(resumeTargetCliFlag(cost.resumeTarget)).toBe('--run-cost-usd N');
  });

  it('非规范载荷（缺键 / undefined）：如实 unavailable，不抛异常崩面板', () => {
    // 修后重审的限定发现：十进制分支原先只挡 `null`，`undefined` 会在 `raw.trim()`
    // 上抛 TypeError。本模块的读数是导出给调用方的纯函数，两种"没有值"必须同一处置。
    expect(dimensionRemaining(undefined, '2.00', true)).toBeNull();
    expect(dimensionRemaining('0.10', undefined, true)).toBeNull();
    expect(dimensionRemaining(null, '2.00', true)).toBeNull();
    expect(dimensionRemaining(undefined, 5, false)).toBeNull();
    expect(dimensionRemaining(3, undefined, false)).toBeNull();
    expect(dimensionRemaining('0.10', '0.30', true)).toBe('0.20');
  });

  it('老暂停载荷（无四维组）：turns 维照旧，其余三维不列（不编 unavailable 噪声）', () => {
    const facts = pauseFacts(pausedInfo());
    expect(facts.dimensions.map((fact) => fact.spec.consumedKey)).toEqual(['agent_turns']);
    expect(facts.tripped?.spec.dimension).toBe('run.max_agent_turns_total');
    expect(targetRunSpec(facts).resumeField).toBe('max_agent_turns_total');
    expect(facts.toolQuotas).toEqual([]);
    expect(facts.trippedTool).toBeNull();
  });

  it('暂停落在 local fuse：没有 run 维读数可报（不拿 turns 冒充），恢复回落到 turns', () => {
    const facts = pauseFacts(pausedInfo({ trigger_dimension: 'local.max_agent_turns' }));
    expect(facts.tripped).toBeNull();
    expect(facts.dimensionLabel).toContain('单次执行保险丝到顶');
    expect(targetRunSpec(facts).resumeField).toBe('max_agent_turns_total');
  });
});

describe('ceilingDraftError —— 输入预校验（后端才是权威，这里只省一次必然 409）', () => {
  it('合法值：≥ consumed + 2（恰好 consumed+1 会被后端 409 拒）', () => {
    const paused = pausedInfo();
    expect(ceilingDraftError(paused, '5')).toBeNull();
    expect(ceilingDraftValue(paused, '5')).toBe(5);
    expect(ceilingDraftValue(paused, ' 8 ')).toBe(8);
    expect(ceilingDraftError(paused, '4')).toContain('至少 5');
    // 0 先被"正整数"那关拦下（0 不是正整数），轮不到"至少 N"——两条都拒绝，顺序不影响结论。
    expect(ceilingDraftError(paused, '0')).toBe('ceiling 必须是正整数');
  });

  it('非正整数 / 空值：当场拒绝，不发请求', () => {
    const paused = pausedInfo();
    expect(ceilingDraftError(paused, '')).toContain('请填');
    expect(ceilingDraftError(paused, '8.5')).toBe('ceiling 必须是正整数');
    expect(ceilingDraftError(paused, 'abc')).toBe('ceiling 必须是正整数');
    expect(ceilingDraftError(paused, '-4')).toBe('ceiling 必须是正整数');
    // 0 先被"正整数"这关拦下（0 不是正整数），轮不到"至少 N"——两条都拒绝，顺序不影响结论。
    expect(ceilingDraftError(paused, '0')).toBe('ceiling 必须是正整数');
    expect(ceilingDraftValue(paused, 'abc')).toBeNull();
  });
});

describe('per-tool 配额（`#314` T6）—— 动态维度、两个 counter、独立恢复目标', () => {
  /** 真实载荷形状（后端 `build_pause_data`）：被 `glob` 的配额卡住，同时配了 `bash`
   *  的配额而它一次都没调过。 */
  function toolPaused(overrides: Partial<RunPausedInfo> = {}): RunPausedInfo {
    return pausedInfo({
      trigger_dimension: 'run.tool_call_limits.glob',
      consumed_agent_turns: 2,
      run_limit: 8,
      consumed_dimensions: {
        agent_turns: 2,
        model_requests: 3,
        total_tokens: 40,
        cost_usd: null,
        // 一次逻辑调用、三次真实尝试（两次 retry）——两个 counter 不是别名。
        tool_calls: 1,
        tool_attempts: 3,
        tool_calls_by_tool: { glob: 1 },
        tool_attempts_by_tool: { glob: 3 },
      },
      run_limits: {
        max_agent_turns_total: 8,
        max_model_requests: null,
        max_total_tokens: null,
        max_cost_usd: null,
        tool_call_limits: { glob: 1, bash: 5 },
      },
      ...overrides,
    });
  }

  it('两个来源的并集、按工具名排序：调过的与配了的都在，读数逐项对应事件真值', () => {
    const quotas = toolQuotaFacts(toolPaused());
    expect(quotas.map((quota) => quota.name)).toEqual(['bash', 'glob']);
    const [bash, glob] = quotas;
    // 配了却一次没调：calls 表里没有这个键 ⇒ unavailable（**不是** 0）。
    // 这是与 CLI `_tool_dimension_lines` 逐字一致的口径（那边也是 `calls.get(name)`
    // 后判 None）——两个客户端显示同一份事实，是票面 AC 的原话。
    expect(bash.callsText).toBe('unavailable');
    expect(bash.ceilingText).toBe('5');
    expect(bash.remainingText).toBe('unavailable');
    expect(bash.tripped).toBe(false);
    // 命中那一维：逻辑调用 1、尝试 3（含 retry）、ceiling 1、剩余 0。
    expect(glob.callsText).toBe('1');
    expect(glob.attemptsText).toBe('3');
    expect(glob.ceilingText).toBe('1');
    expect(glob.remainingText).toBe('0');
    expect(glob.tripped).toBe(true);
  });

  it('暂停落在工具配额：tripped（run 维）为 null、恢复目标指那个工具（不拿 turns 冒充）', () => {
    const facts = pauseFacts(toolPaused());
    expect(facts.tripped).toBeNull();
    expect(facts.trippedTool?.name).toBe('glob');
    expect(facts.resumeTarget).toEqual({ kind: 'tool', quota: facts.trippedTool });
    expect(resumeTargetLabel(facts.resumeTarget)).toBe('tool_call_limits.glob');
    // CLI 的开关是**真实 argv 形状**（`NAME=N` 一个参数），与 `resume_hint` 逐字一致。
    expect(resumeTargetCliFlag(facts.resumeTarget)).toBe('--run-tool-limit glob=N');
    // 四维读数照旧在清单里（工具配额是第五类事实，不替换 turns 那一行）。
    expect(facts.dimensions[0].consumedText).toBe('2');
  });

  it('工具配额的最小合法 ceiling = calls + 1（**不**含 closeout 预留：closeout 不调工具）', () => {
    const facts = pauseFacts(toolPaused());
    expect(minToolQuotaValue(facts.trippedTool!)).toBe(2);
    // 恢复请求的目标与草稿默认值都落在那个工具上。
    expect(resumeRequestTarget(toolPaused())).toEqual({ kind: 'tool', tool: 'glob' });
    expect(defaultResumeDraft(toolPaused())).toBe('2');
    expect(resumeInputHint(facts)).toContain('抬的是 tool_call_limits.glob');
    expect(resumeInputHint(facts)).toContain('--run-tool-limit glob=N');
    expect(resumeInputHint(facts)).toContain('至少 2');
  });

  it('草稿预校验：正整数、且严格大于已接纳的调用数（恰好等于会被后端 409 拒）', () => {
    const paused = toolPaused();
    expect(ceilingDraftError(paused, '2')).toBeNull();
    expect(ceilingDraftValue(paused, '2')).toBe(2);
    expect(ceilingDraftError(paused, '1')).toContain('必须大于已消耗 1 次调用');
    expect(ceilingDraftError(paused, '1')).toContain('至少 2');
    // 形状先于"够不够"：0 不是正整数，不该以"必须大于已消耗"的面目出现。
    expect(ceilingDraftError(paused, '0')).toBe('ceiling 必须是正整数');
    expect(ceilingDraftError(paused, '2.5')).toBe('ceiling 必须是正整数');
    expect(ceilingDraftError(paused, '')).toContain('请填绝对 ceiling');
    expect(ceilingDraftValue(paused, 'abc')).toBeNull();
  });

  it('没配 per-tool 配额：只列调用过的工具，limit 写 unlimited（不是 0）', () => {
    const facts = pauseFacts(
      toolPaused({
        trigger_dimension: 'run.max_agent_turns_total',
        run_limits: {
          max_agent_turns_total: 8,
          max_model_requests: null,
          max_total_tokens: null,
          max_cost_usd: null,
          tool_call_limits: {},
        },
      }),
    );
    expect(facts.trippedTool).toBeNull();
    expect(facts.toolQuotas.map((quota) => quota.name)).toEqual(['glob']);
    expect(facts.toolQuotas[0].ceilingText).toBe('unlimited');
    expect(facts.toolQuotas[0].remainingText).toBe('unavailable');
    // 命中 turns 维时恢复目标照旧是 turns（工具配额只是清单里的事实）。
    expect(targetRunSpec(facts).resumeField).toBe('max_agent_turns_total');
  });

  it('账目未知（T6 之前的快照）：读数是 unavailable，不是 0', () => {
    // 快照在、两张工具表缺席（`#314` 之前落的 run/paused）。
    const pausedUnknown = toolPaused({
      consumed_dimensions: {
        agent_turns: 2,
        model_requests: 3,
        total_tokens: 40,
        cost_usd: null,
        tool_calls: null,
        tool_attempts: null,
        tool_calls_by_tool: null,
        tool_attempts_by_tool: null,
      },
    });
    const facts = pauseFacts(pausedUnknown);
    // calls 表未知 ⇒ 只列配了配额的工具名（glob / bash），读数一律 unavailable。
    expect(facts.toolQuotas.map((quota) => quota.name)).toEqual(['bash', 'glob']);
    expect(facts.toolQuotas.map((quota) => quota.callsText)).toEqual([
      'unavailable', 'unavailable',
    ]);
    // 账目未知时算不出最小合法值 ⇒ 草稿默认值给 null（调用方渲染空串，不编数字），
    // 草稿预校验如实说"这一维配了 ceiling 而账目未知 ⇒ 后端必然 409"。
    expect(minToolQuotaValue(facts.trippedTool!)).toBeNull();
    expect(defaultResumeDraft(pausedUnknown)).toBeNull();
    expect(ceilingDraftError(pausedUnknown, '2')).toContain('账目未知');
  });
});
