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
import { ceilingDraftError, ceilingDraftValue, dimensionLabel, pauseFacts } from './runBudget';
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
    expect(facts.minResumeCeiling).toBe(5); // consumed + 2（后端判据 > consumed + 1）
  });

  it('未配 run ceiling：ceiling/remaining 都是 null（渲染成 unlimited，不是 0）', () => {
    const facts = pauseFacts(pausedInfo({ run_limit: null, local_fuse: null }));
    expect(facts.ceiling).toBeNull();
    expect(facts.remaining).toBeNull();
    expect(facts.localFuseTurns).toBeNull();
  });

  it('两个作用域的标签各自认得出来；未知维度原样回显（不编名字）', () => {
    expect(dimensionLabel('run.max_agent_turns_total')).toContain('run 累计轮次到顶');
    expect(dimensionLabel('local.max_agent_turns')).toContain('单次执行保险丝到顶');
    expect(dimensionLabel('run.max_total_tokens')).toBe('run.max_total_tokens');
    expect(dimensionLabel('')).toBe('预算维度未声明');
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
