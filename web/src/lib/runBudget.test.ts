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
  deadlineDraftError,
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
    // 配了却一次没调：表**已知**（这里它只含 `glob`）⇒ 缺名就是 0，不是 unavailable。
    // 口径与后端 `BudgetConsumed.calls_for` 逐字同源（表在 ⇒ 缺名 = 0），也与 CLI
    // `_tool_dimension_lines` 同源——两轴审查发现过这一格曾两边一致地读错：配了 ceiling
    // 却从未调用过的工具被报成 unavailable，而同一份事件的服务端投影给的是 remaining=5。
    expect(bash.callsText).toBe('0');
    expect(bash.attemptsText).toBe('0');
    expect(bash.ceilingText).toBe('5');
    expect(bash.remainingText).toBe('5');
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


describe('deadline 暂停（`#315` T7）—— 判的是时刻，不是"consumed 与 ceiling 比大小"', () => {
  /** 真实载荷形状：到点那一刻落下的 `run/paused`。 */
  function deadlinePaused(overrides: Partial<RunPausedInfo> = {}): RunPausedInfo {
    return pausedInfo({
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

  it('恢复目标指 deadline 那一维，且读数行里没有"某一维到顶"可报', () => {
    const facts = pauseFacts(deadlinePaused());
    expect(facts.deadlinePause).toBe(true);
    expect(facts.deadline).toBe('2026-09-26T04:10:00Z');
    expect(facts.resumeTarget.kind).toBe('deadline');
    // 到点不是"某一维的 consumed 撞到 ceiling"——拿 turns 冒充会给出一个假读数行。
    expect(facts.tripped).toBeNull();
    // 四维清单照旧完整（turns 等仍是事实，只是都不是卡住的那一维）。
    expect(facts.dimensions.map((row) => row.spec.dimension)).toContain(
      'run.max_agent_turns_total',
    );
  });

  it('原因与维度的标签各自成句：deadline 不是 budget_exhausted 的另一种写法', () => {
    const facts = pauseFacts(deadlinePaused());
    expect(facts.reasonLabel).toContain('deadline');
    expect(facts.dimensionLabel).toContain('run.deadline_at');
    // 恢复提示点名"换一个未来时刻"，且给出的是 CLI 的**真实 argv**（开关 + 时刻）。
    const hint = resumeInputHint(facts);
    expect(hint).toContain('未来');
    expect(hint).toContain('2026-09-26T04:10:00Z');
    expect(hint).toContain('--run-deadline 2026-09-26T04:30:00Z');
    expect(resumeTargetCliFlag(facts.resumeTarget)).toBe('--run-deadline 2026-09-26T04:30:00Z');
  });

  it('草稿默认值给 null：本模块**不编**"现在 + N 分钟"这种策略', () => {
    expect(defaultResumeDraft(deadlinePaused())).toBeNull();
  });

  it('草稿校验：空 / 朴素时间 / 已过去 / 非法形状各自被拒，未来时刻放行', () => {
    const paused = deadlinePaused();
    expect(ceilingDraftError(paused, '')).toContain('未来时刻');
    // 朴素时间：后端 422（`parse_deadline_at` 拒无时区）——前端不该放它过去。
    expect(ceilingDraftError(paused, '2099-01-01T00:00:00')).toContain('时区');
    expect(ceilingDraftError(paused, '不是时刻')).toContain('时区');
    // 已过去（含"沿用本次那个时刻"）：后端 409（`resume_headroom_ok` 要严格未来）。
    expect(ceilingDraftError(paused, '2026-01-01T00:00:00Z')).toContain('未来');
    expect(ceilingDraftError(paused, '2026-09-26T04:10:00Z')).toContain('未来');
    expect(ceilingDraftError(paused, '2099-01-01T00:00:00Z')).toBeNull();
    // 带偏移的写法也收（后端归一化到 UTC 存），瞬时在未来即可。
    expect(ceilingDraftError(paused, '2099-01-01T08:00:00+08:00')).toBeNull();
    // 合法值原样提交（时刻是**文本**，不经过数字通道）。
    expect(ceilingDraftValue(paused, '2099-01-01T00:00:00Z')).toBe('2099-01-01T00:00:00Z');
    expect(ceilingDraftValue(paused, '2099-01-01T00:00:00')).toBeNull();
  });

  it('小写 z 要拒：后端只认大写 Z（`fromisoformat` 对小写抛 ValueError ⇒ 422）', () => {
    // 这一格极易漏：`Date.parse` **收**小写 z（比 ES 规范宽），所以"交给它判"会放行，
    // 而后端 `datetime.fromisoformat('…z')` 抛 ValueError ⇒ 一次必然 422 的往返。
    //
    // 反向的一格**不**跟：小写 `t` 与空格分隔符后端其实都收（`fromisoformat` 比 RFC
    // 更宽），前端只是更保守地要求 `T`——那是形状口味，不是"必然被拒"。**只有大小写
    // 的 Z 这一格是"前端放行 ⇒ 后端 422"的真分叉**（实测：Python 3.13）。
    const paused = deadlinePaused();
    expect(deadlineDraftError(paused, '2099-01-01T00:00:00z')).toContain('时区');
    // 大写 Z 与显式偏移照旧放行（偏移那一支与 z 的大小写无关）。
    expect(deadlineDraftError(paused, '2099-01-01T00:00:00Z')).toBeNull();
    expect(deadlineDraftError(paused, '2099-01-01T08:00:00+08:00')).toBeNull();
  });

  it('恢复请求目标给的是字段名 deadline_at（不是某个 ceiling 字段）', () => {
    expect(resumeRequestTarget(deadlinePaused())).toEqual({
      kind: 'deadline', field: 'deadline_at',
    });
  });

  it('快照里没带时刻：如实说"没有时刻"，不编一个', () => {
    const bumped = deadlinePaused({
      run_limits: {
        max_agent_turns_total: 8,
        max_model_requests: null,
        max_total_tokens: null,
        max_cost_usd: null,
        deadline_at: null,
        tool_call_limits: {},
      },
    });
    const facts = pauseFacts(bumped);
    expect(facts.deadline).toBeNull();
    // 原因仍是 deadline（它是**暂停原因**，与"快照里有没有时刻"是两件事）。
    expect(facts.deadlinePause).toBe(true);
    expect(resumeInputHint(facts)).toContain('没有时刻');
    expect(deadlineDraftError(bumped, '2026-01-01T00:00:00Z')).toContain('未来');
  });
});
