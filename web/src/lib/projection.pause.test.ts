/** `run/paused` / `run/resumed` 的投影契约（`#312` T4）。
 *
 *  锁的是**四件在票面上明写的事**：
 *  1. 暂停是**非终态**：run_status='paused'，不产生 completed/failed 的投影后果；
 *  2. 暂停把当前执行区间收口（caret 停、running 工具标 stopped）——所以历史轮次
 *     不再"看起来还在跑"；
 *  3. `run/resumed` 收起暂停事实并把状态放回 running，且**不改** consumed（恢复不重置）；
 *  4. 重放幂等：同一段事件逐帧应用与 projectHistory 重建得到同一投影（刷新后一致）。
 *
 *  载荷取真实形状（后端 `run_budget.build_pause_data` / `build_resume_data`）——
 *  用别处发明同义词的假载荷测，等于测自己编的契约。 */

import { describe, expect, it } from 'vitest';
import { EventType } from '../types';
import type { AgentEvent } from '../types';
import { applyEvent, initConversation, projectHistory, summarizeEvent } from './projection';
import { deadlineInstant } from './runBudget';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  // 恒带 `time`：真实事件都带（durable log 的 SessionEvent.time）。夹具省掉它会让
  // `finalizeRun` 落到 `new Date()` 兜底——于是"逐帧应用"与"整段重建"两条路径的
  // completed_at 差几毫秒，幂等断言变成测时钟精度（已实测踩过）。
  return { data: {}, seq: null, run_id: 'run-1', step_id: null, time: '2026-09-25T00:00:00.000Z', ...partial };
}

function pausedEvent(seq = 9): AgentEvent {
  return ev({
    type: EventType.RUN_PAUSED,
    seq,
    step_id: 2,
    data: {
      reason: 'budget_exhausted',
      trigger_dimension: 'run.max_agent_turns_total',
      budget_version: 1,
      consumed: { agent_turns: 3 },
      limits: {
        local: { max_agent_turns: 500, source: 'deployment' },
        run: { max_agent_turns_total: 4 },
      },
      continuation: {
        completed: ['本逻辑 run 已消耗 3 个 agent turn'],
        remaining: ['暂停发生在下一轮模型决策之前'],
        blockers: ['run.max_agent_turns_total 到顶：consumed=3, ceiling=4'],
        next_safe_action: '提高绝对 ceiling 后以同一 run_id 恢复',
      },
      closeout_source: 'model',
      resume_requirements: [],
      trace_id: 'trace-abc',
    },
  });
}

function resumedEvent(seq = 12): AgentEvent {
  return ev({
    type: EventType.RUN_RESUMED,
    seq,
    data: {
      from_pause_seq: 9,
      previous_budget_version: 1,
      budget_version: 2,
      limits: {
        local: { max_agent_turns: 500, source: 'deployment' },
        run: { max_agent_turns_total: 8 },
      },
      consumed: { agent_turns: 3 },
      resume_basis: 'budget_increase',
    },
  });
}

/** 到暂停为止的一段真实事件序列（user → run/started → 两轮模型 → 暂停）。 */
function upToPause(): AgentEvent[] {
  return [
    ev({ type: EventType.USER_MESSAGE, seq: 1, data: { content: '改一下配置' } }),
    ev({ type: EventType.RUN_STARTED, seq: 2, data: { turn_index: 1, model: 'glm-5.3' } }),
    ev({ type: EventType.MODEL_STARTED, seq: 3, step_id: 1, data: { step: 1 } }),
    ev({ type: EventType.MODEL_COMPLETED, seq: 4, step_id: 1, data: { content: 'A 已改完' } }),
    ev({ type: EventType.TOOL_CALL, seq: 5, step_id: 1, data: { tool_call_id: 't1', tool_name: 'bash' } }),
    ev({ type: EventType.TOOL_RESULT, seq: 6, step_id: 1, data: { tool_call_id: 't1', content: 'ok' } }),
    ev({ type: EventType.MODEL_STARTED, seq: 7, step_id: 2, data: { step: 2 } }),
    ev({ type: EventType.MODEL_COMPLETED, seq: 8, step_id: 2, data: { content: 'B 也改完' } }),
    pausedEvent(9),
  ];
}

describe('run/paused — 非终态暂停的投影（#312）', () => {
  it('折叠出完整的暂停事实：version / consumed / ceiling / continuation 全部来自事件', () => {
    const s = projectHistory('s', upToPause());
    const paused = s.run_paused;
    expect(paused).not.toBeNull();
    expect(paused?.run_id).toBe('run-1');
    expect(paused?.version).toBe(1);
    expect(paused?.pause_seq).toBe(9);
    expect(paused?.reason).toBe('budget_exhausted');
    expect(paused?.trigger_dimension).toBe('run.max_agent_turns_total');
    expect(paused?.consumed_agent_turns).toBe(3);
    expect(paused?.run_limit).toBe(4);
    expect(paused?.local_fuse).toEqual({ max_agent_turns: 500, source: 'deployment' });
    expect(paused?.closeout_source).toBe('model');
    expect(paused?.resume_requirements).toEqual([]);
    expect(paused?.trace_id).toBe('trace-abc');
    expect(paused?.continuation?.next_safe_action).toContain('提高绝对 ceiling');
  });

  it('run_status 收成 paused —— 不是 completed / failed / interrupted', () => {
    const s = projectHistory('s', upToPause());
    expect(s.run_status).toBe('paused');
    expect(s.run_failure).toBeNull();
    expect(s.run_interrupted).toBeNull();
  });

  it('暂停收口当前执行区间：caret 停、running 工具标 stopped（中断 ≠ 失败，也不让它闪）', () => {
    const events = upToPause();
    // 让最后一个工具停在 running：把它的 result 换成"还没回来"的形态
    const withRunningTool = [
      ...events.slice(0, 4),
      ev({ type: EventType.TOOL_CALL, seq: 5, step_id: 1, data: { tool_call_id: 't1', tool_name: 'bash' } }),
      ev({ type: EventType.MODEL_STARTED, seq: 7, step_id: 2, data: { step: 2 } }),
      pausedEvent(9),
    ];
    const s = projectHistory('s', withRunningTool);
    expect(s.active_step_id).toBeNull();
    expect(s.turns[0].tools[0].status).toBe('stopped');
    expect(s.turns[1].model.status).toBe('done');
  });

  it('缺数字 / 畸形 continuation：如实留 null 与 0，不伪造进度（零伪造）', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.RUN_PAUSED,
      seq: 3,
      data: {
        // 没有 limits / consumed / continuation / budget_version
        reason: 'budget_exhausted',
        trigger_dimension: 'local.max_agent_turns',
      },
    }));
    expect(s.run_status).toBe('paused');
    expect(s.run_paused?.run_limit).toBeNull();
    expect(s.run_paused?.local_fuse).toBeNull();
    expect(s.run_paused?.consumed_agent_turns).toBe(0);
    expect(s.run_paused?.continuation).toBeNull();
    expect(s.run_paused?.version).toBe(1); // 版本缺失 = 首次，CAS 仍可比较
    expect(s.run_paused?.trace_id).toBeNull();
  });

  it('continuation 形状不合契约（缺 next_safe_action）⇒ 整体 null，不补空壳', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.RUN_PAUSED,
      seq: 3,
      data: { continuation: { completed: [], remaining: [], blockers: [] } },
    }));
    expect(s.run_paused?.continuation).toBeNull();
  });
});

/** `#314`：被某个工具配额卡住的暂停（真实载荷形状：
 *  `consumed.tool_calls_by_tool` × `limits.run.tool_call_limits`）。 */
function toolQuotaPausedEvent(seq = 9): AgentEvent {
  return ev({
    type: EventType.RUN_PAUSED,
    seq,
    step_id: 2,
    data: {
      reason: 'budget_exhausted',
      trigger_dimension: 'run.tool_call_limits.glob',
      budget_version: 1,
      consumed: {
        agent_turns: 2,
        model_requests: 3,
        total_tokens: 40,
        cost_usd: null,
        tool_calls: 1,
        tool_attempts: 3,
        tool_calls_by_tool: { glob: 1 },
        tool_attempts_by_tool: { glob: 3 },
      },
      limits: {
        local: { max_agent_turns: 500, source: 'deployment' },
        run: {
          max_agent_turns_total: 8,
          tool_call_limits: { glob: 1 },
        },
      },
      continuation: {
        completed: ['本逻辑 run 已消耗 1 次 glob 调用'],
        remaining: ['暂停发生在下一轮模型决策之前'],
        blockers: ['run.tool_call_limits.glob 到顶：consumed=1, ceiling=1'],
        next_safe_action: '提高该工具的绝对配额后以同一 run_id 恢复',
      },
      closeout_source: 'model',
      resume_requirements: [],
      trace_id: 'trace-tool',
    },
  });
}

describe('run/paused 的 per-tool 事实（`#314` T6）', () => {
  it('两个 counter 与两张表逐字解析（calls ≠ attempts，各自独立）', () => {
    const s = projectHistory('s', [toolQuotaPausedEvent()]);
    const paused = s.run_paused;
    expect(paused?.trigger_dimension).toBe('run.tool_call_limits.glob');
    expect(paused?.consumed_dimensions?.tool_calls).toBe(1);
    expect(paused?.consumed_dimensions?.tool_attempts).toBe(3);
    expect(paused?.consumed_dimensions?.tool_calls_by_tool).toEqual({ glob: 1 });
    expect(paused?.consumed_dimensions?.tool_attempts_by_tool).toEqual({ glob: 3 });
    expect(paused?.run_limits?.tool_call_limits).toEqual({ glob: 1 });
    // turns 维照旧（它是同一份快照里的另一维，不是"上一维"）。
    expect(paused?.consumed_dimensions?.agent_turns).toBe(2);
  });

  it('"表缺席"与"表为空"可分辨：前者 null（未知），后者 {}（一个都没调/没配）', () => {
    const missing = applyEvent(initConversation('s'), ev({
      type: EventType.RUN_PAUSED,
      seq: 3,
      data: { consumed: { agent_turns: 1 }, limits: { run: { max_agent_turns_total: 2 } } },
    }));
    expect(missing.run_paused?.consumed_dimensions?.tool_calls_by_tool).toBeNull();
    expect(missing.run_paused?.run_limits?.tool_call_limits).toBeNull();

    const empty = applyEvent(initConversation('s'), ev({
      type: EventType.RUN_PAUSED,
      seq: 3,
      data: {
        consumed: { agent_turns: 1, tool_calls_by_tool: {} },
        limits: { run: { max_agent_turns_total: 2, tool_call_limits: {} } },
      },
    }));
    expect(empty.run_paused?.consumed_dimensions?.tool_calls_by_tool).toEqual({});
    expect(empty.run_paused?.run_limits?.tool_call_limits).toEqual({});
  });

  it('非整数 / 负数条目整条丢掉（编不出来的计数不冒充 0）', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.RUN_PAUSED,
      seq: 3,
      data: {
        consumed: { tool_calls_by_tool: { glob: 2, broken: 'x', neg: -1, frac: 1.5 } },
        limits: { run: { tool_call_limits: { glob: 3 } } },
      },
    }));
    expect(s.run_paused?.consumed_dimensions?.tool_calls_by_tool).toEqual({ glob: 2 });
  });

  it('ceiling 表的下限是 1（计数表是 0）：`0` / 负数 / 小数都不收', () => {
    // 两张表用**不同**下限：计数表放行 0（"调了 0 次"是事实），ceiling 表要求 ≥ 1
    // （后端 `parse_tool_call_limits` 只收正整数 ⇒ `{"glob": 0}` 那个入口必然 422）。
    // 共用一个下限会让前端收下一个后端永远不可能落盘的 ceiling，面板于是显示一个
    // 不存在于任何事件里的读数（两轴审查 Standards 面发现）。
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.RUN_PAUSED,
      seq: 3,
      data: {
        consumed: { tool_calls_by_tool: { zero: 0, glob: 2 } },
        limits: {
          run: { tool_call_limits: { glob: 3, zero: 0, neg: -1, frac: 2.5 } },
        },
      },
    }));
    expect(s.run_paused?.run_limits?.tool_call_limits).toEqual({ glob: 3 });
    // 同一份载荷里的**计数**表照旧保留 0——两处分界各自成立，不是一刀切。
    expect(s.run_paused?.consumed_dimensions?.tool_calls_by_tool).toEqual({ zero: 0, glob: 2 });
  });

  it('重放幂等：逐帧应用与 projectHistory 重建同一投影（刷新/重连后一致）', () => {
    const events = [
      ev({ type: EventType.RUN_STARTED, seq: 1, data: { turn_index: 1 } }),
      toolQuotaPausedEvent(9),
    ];
    let stepped = initConversation('s');
    for (const e of events) stepped = applyEvent(stepped, e);
    expect(projectHistory('s', events).run_paused).toEqual(stepped.run_paused);
  });

  it('Timeline 摘要报**那个工具**的调用数与它的 ceiling（不是 turns 的那一对）', () => {
    expect(summarizeEvent(toolQuotaPausedEvent())).toBe(
      'run.tool_call_limits.glob · 1 次调用/上限 1',
    );
  });
});

/** `#315`：deadline 到点的暂停（真实载荷形状：`limits.run.deadline_at` 是 RFC 3339 UTC
 *  **文本**，与四个 `max_*` 同住一张表）。 */
function deadlinePausedEvent(
  run: Record<string, unknown> = { max_agent_turns_total: 8, deadline_at: '2026-09-26T04:10:00Z' },
): AgentEvent {
  return ev({
    type: EventType.RUN_PAUSED,
    seq: 9,
    step_id: 2,
    data: {
      reason: 'deadline',
      trigger_dimension: 'run.deadline_at',
      budget_version: 1,
      consumed: {
        agent_turns: 2,
        model_requests: 3,
        total_tokens: 40,
        cost_usd: null,
        tool_calls: 1,
        tool_attempts: 1,
        tool_calls_by_tool: { bash: 1 },
        tool_attempts_by_tool: { bash: 1 },
      },
      limits: {
        local: { max_agent_turns: 500, source: 'deployment' },
        run,
      },
      continuation: {
        completed: ['到点前已完成的工作'],
        remaining: ['暂停发生在回合之间的稳定边界'],
        blockers: ['run.deadline_at 到点：2026-09-26T04:10:00Z'],
        next_safe_action: '换一个未来时刻后以同一 run_id 恢复',
      },
      closeout_source: 'deterministic',
      resume_requirements: [],
      trace_id: 'trace-deadline',
    },
  });
}

describe('run/paused 的 deadline 事实（`#315` T7）', () => {
  it('limits.run.deadline_at 逐字进投影，面板读得到它（事件 → 投影 → 消费端一条链）', () => {
    // 这一格是 `#315` 的 `tsc -b` 抓回来的：类型加了 `deadline_at`，而解析器没读它
    // ⇒ `run_limits.deadline_at` 在生产路径上恒为 undefined，面板的"绝对截止时刻"
    // 一行永远拿不到值。手工构造 `RunPausedInfo` 的用例看不见这一格，所以钉在**投影**上。
    const s = projectHistory('s', [deadlinePausedEvent()]);
    expect(s.run_paused?.trigger_dimension).toBe('run.deadline_at');
    expect(s.run_paused?.run_limits?.deadline_at).toBe('2026-09-26T04:10:00Z');
    expect(deadlineInstant(s.run_paused!)).toBe('2026-09-26T04:10:00Z');
  });

  it('整键缺席 ⇒ null（没配 deadline），不是空串、也不是编出来的时刻', () => {
    const s = projectHistory('s', [deadlinePausedEvent({ max_agent_turns_total: 8 })]);
    expect(s.run_paused?.run_limits?.deadline_at).toBeNull();
    expect(s.run_paused?.run_limits?.max_agent_turns_total).toBe(8);
  });

  it('数 / 空串 **不**当时刻收下（后端 `parse_deadline_at` 收不了它们）', () => {
    // cost 维的读法（数也当读数）在 deadline 维是错的：`0` 是 epoch 秒还是毫秒？
    // 后端只认带时区的文本 ⇒ 前端把它收成时刻等于编一个任何事件里都不存在的读数。
    const asNumber = projectHistory('s', [deadlinePausedEvent({ deadline_at: 0 })]);
    expect(asNumber.run_paused?.run_limits?.deadline_at).toBeNull();
    const blank = projectHistory('s', [deadlinePausedEvent({ deadline_at: '  ' })]);
    expect(blank.run_paused?.run_limits?.deadline_at).toBeNull();
    const trimmed = projectHistory('s', [deadlinePausedEvent({ deadline_at: ' 2026-09-26T04:10:00Z ' })]);
    expect(trimmed.run_paused?.run_limits?.deadline_at).toBe('2026-09-26T04:10:00Z');
  });
});

describe('run/resumed — 同 run 恢复的投影（#312）', () => {
  it('收起暂停事实并把状态放回 running；consumed 不变（恢复不重置）', () => {
    const paused = projectHistory('s', upToPause());
    const resumed = applyEvent(paused, resumedEvent(12));
    expect(resumed.run_paused).toBeNull();
    expect(resumed.run_status).toBe('running');
    // 消耗是账本事实，不在前端重算：暂停快照 3 就还是 3（后续 model/completed 另加）
    expect(paused?.run_paused?.consumed_agent_turns).toBe(3);
  });

  it('恢复后的新工作进新轮（step_base+1），已暂停的那一轮保持可读', () => {
    let s = projectHistory('s', upToPause());
    s = applyEvent(s, resumedEvent(12));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, seq: 13, step_id: 3, data: { step: 3 } }));
    s = applyEvent(s, ev({ type: EventType.MODEL_COMPLETED, seq: 14, step_id: 3, data: { content: '继续做完' } }));
    expect(s.turns).toHaveLength(3);
    expect(s.turns[0].model.text).toBe('A 已改完');
    expect(s.turns[1].model.text).toBe('B 也改完');
    expect(s.turns[2].model.text).toBe('继续做完');
    expect(s.turns[2].user_message).toBe(''); // 同 run 恢复没有新 user/message
  });

  it('重放幂等：逐帧应用与 projectHistory 重建同一投影（刷新后一致）', () => {
    const events = [...upToPause(), resumedEvent(12)];
    let stepped = initConversation('s');
    for (const e of events) stepped = applyEvent(stepped, e);
    const rebuilt = projectHistory('s', events);
    expect(rebuilt.run_status).toBe(stepped.run_status);
    expect(rebuilt.run_paused).toEqual(stepped.run_paused);
    expect(rebuilt.turns).toEqual(stepped.turns);
  });

  it('暂停后又落终态（异常路径）：终态胜——暂停事实不再挂着', () => {
    let s = projectHistory('s', upToPause());
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED, seq: 12, data: {} }));
    expect(s.run_status).toBe('completed');
    // run_paused 仍在（它陈述的是"曾经暂停过、已被终态终结"的历史事实，
    // 但 UI 的入口由 run_status 把门：canResumePaused 要求非流式 + 暂停面板只看
    // run_status='paused'——所以这里不额外清理，避免引入"两处都要记得清"的状态）。
    expect(s.run_status).not.toBe('paused');
  });
});

describe('summarizeEvent — 暂停/恢复的 Timeline 单行语义（#312）', () => {
  it('run/paused 给维度 + consumed/ceiling；run/resumed 给版本跃迁', () => {
    expect(summarizeEvent(pausedEvent())).toBe('run.max_agent_turns_total · 3/4');
    expect(summarizeEvent(resumedEvent())).toBe('已恢复 · 预算版本 1 → 2');
  });
});
