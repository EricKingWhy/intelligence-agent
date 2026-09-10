import { describe, expect, it } from 'vitest';
import { RUN_TERMINAL_TYPES, deriveRunPulse, hasUnterminatedRun, isRecoverableRun, recoverDoneMessage, unpairedToolCallIds } from './runState';
import { initConversation, applyEvent } from './projection';
import { EventType } from '../types';
import type { AgentEvent } from '../types';

function ev(type: string, data: Record<string, unknown>, step_id: number | null = 1): AgentEvent {
  return { type, data, seq: null, run_id: null, step_id };
}

describe('deriveRunPulse', () => {
  it('idle when no conversation', () => {
    expect(deriveRunPulse(null, false).state).toBe('idle');
  });

  it('thinking while model streams, tool while tool running', () => {
    let s = initConversation('s1');
    s = applyEvent(s, ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.MODEL_STARTED, {}, 1));
    s = applyEvent(s, ev(EventType.MODEL_DELTA, { delta: 'hi' }, 1));
    expect(deriveRunPulse(s, true).state).toBe('thinking');

    s = applyEvent(
      s,
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }, 1),
    );
    expect(deriveRunPulse(s, true).state).toBe('tool');
    expect(deriveRunPulse(s, true).label).toBe('执行工具');

    s = applyEvent(
      s,
      ev(
        EventType.TOOL_RESULT,
        {
          tool_call_id: 't1',
          content: JSON.stringify({ ok: true, message: 'done', data: null }),
        },
        1,
      ),
    );
    expect(deriveRunPulse(s, true).state).toBe('thinking');
  });

  it('completed / failed terminal states', () => {
    let s = initConversation('s1');
    s = applyEvent(s, ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.RUN_COMPLETED, {}));
    expect(deriveRunPulse(s, false).state).toBe('completed');

    let f = initConversation('s1');
    f = applyEvent(f, ev(EventType.RUN_STARTED, {}));
    f = applyEvent(f, ev(EventType.RUN_FAILED, {}));
    expect(deriveRunPulse(f, false).state).toBe('failed');
    expect(deriveRunPulse(f, false).label).toBe('失败');
  });

  it('idle run_status + not streaming stays idle', () => {
    expect(deriveRunPulse(initConversation('s1'), false).state).toBe('idle');
  });

  it('idle run_status + streaming shows thinking (pre-run/started frames)', () => {
    expect(deriveRunPulse(initConversation('s1'), true).state).toBe('thinking');
    expect(deriveRunPulse(initConversation('s1'), true).label).toBe('思考中');
  });
});

// ── da394a9 批：取消态脉冲 + 恢复可见性 ──

describe('deriveRunPulse — cancelled 通道', () => {
  it('run/failed + run_cancelled → 已取消（中性）而非失败（红）', () => {
    let s = applyEvent(initConversation('s'), ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.RUN_FAILED, { reason: 'cancelled' }));
    const pulse = deriveRunPulse(s, false);
    expect(pulse.state).toBe('cancelled');
    expect(pulse.label).toBe('已取消');
    expect(pulse.className).toBe('pulse-cancelled');
  });

  it('run/failed 无取消标记 → 失败（红）', () => {
    let s = applyEvent(initConversation('s'), ev(EventType.RUN_STARTED, {}));
    s = applyEvent(s, ev(EventType.RUN_FAILED, {}));
    expect(deriveRunPulse(s, false).state).toBe('failed');
  });
});

describe('isRecoverableRun — 恢复入口可见性（da394a9 §二.2）', () => {
  it('run/started 无终态 → true（中断会话可恢复）', () => {
    const events = [ev(EventType.RUN_STARTED, {})];
    expect(isRecoverableRun(events)).toBe(true);
  });

  it('干净完成 → false；干净失败 → false（终态，不可恢复）', () => {
    const done = [ev(EventType.RUN_STARTED, {}), ev(EventType.RUN_COMPLETED, {})];
    const failed = [ev(EventType.RUN_STARTED, {}), ev(EventType.RUN_FAILED, {})];
    expect(isRecoverableRun(done)).toBe(false);
    expect(isRecoverableRun(failed)).toBe(false);
  });

  it('干净失败 + 未配对 tool_call → true（dangling 可修）', () => {
    const events = [
      ev(EventType.RUN_STARTED, {}),
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }),
      // t1 的 tool/result 丢了（dangling）
      ev(EventType.TOOL_CALL, { tool_call_id: 't2', tool_name: 'bash' }),
      ev(EventType.TOOL_RESULT, { tool_call_id: 't2', content: 'ok' }),
      ev(EventType.RUN_FAILED, {}),
    ];
    expect(isRecoverableRun(events)).toBe(true);
  });

  it('无 run 的裸会话 → false（无可恢复物）', () => {
    expect(isRecoverableRun([ev(EventType.SESSION_STARTED, {})])).toBe(false);
  });
});

// ── T8 run/interrupted 必须算终态（否则崩溃会话修好后按钮永不消失）──

describe('isRecoverableRun — run/interrupted 是终态', () => {
  it('run/interrupted 且无 dangling → false（后端已按 Ledger 回填完，入口必须消失）', () => {
    const events = [
      ev(EventType.RUN_STARTED, {}),
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }),
      ev(EventType.TOOL_RESULT, { tool_call_id: 't1', content: 'ok' }),
      ev(EventType.RUN_INTERRUPTED, { interrupted_seq: 3, reason: 'process_restart' }),
    ];
    expect(isRecoverableRun(events)).toBe(false);
  });

  it('run/interrupted + 未配对 tool_call → true（仍有 dangling 可修）', () => {
    const events = [
      ev(EventType.RUN_STARTED, {}),
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }),
      // t1 的 tool/result 丢了（dangling）
      ev(EventType.RUN_INTERRUPTED, { interrupted_seq: 2, reason: 'process_restart' }),
    ];
    expect(isRecoverableRun(events)).toBe(true);
  });

  it('RUN_TERMINAL_TYPES 锁本端字面集合（三处枚举的唯一事实源）', () => {
    // 防止这里被误删/误加——三处枚举曾各自漏掉 run/interrupted。
    // 「后端新增第四种终态」这一侧由下面独立的 EventType 词表漂移锁接住。
    expect([...RUN_TERMINAL_TYPES].sort()).toEqual([
      EventType.RUN_COMPLETED,
      EventType.RUN_FAILED,
      EventType.RUN_INTERRUPTED,
    ]);
  });
});

// ── EventType 词表漂移锁（跨 worktree 的唯一可用信号：生成物）──

describe('EventType 词表漂移锁', () => {
  it('生成物里每个 run 事件都必须被显式分类（终态或 run/started）', () => {
    // 单测读不到另一个 worktree 的后端源码，但**读得到生成物**
    // （web/src/generated/event-types.ts 由后端 scripts/gen_event_types.py 生成）。
    // 后端加第四种 run 终态时会重新生成它，这里的「有未分类的 run 事件」随即变红，
    // 逼着维护者回答「它是不是终态」——T8 加 run/interrupted 时三处枚举集体漏掉，
    // 正是因为没有这道门（已 mutation 验证：注入 run/paused → 本测试变红）。
    // 已分类 = 终态集合里的，或已知非终态的 run/started。
    const runTypes: string[] = Object.values(EventType).filter((v) => v.startsWith('run/'));
    expect(runTypes.length).toBeGreaterThan(1); // 生成物没读到时应立刻失败，而不是静默通过
    const unclassified = runTypes.filter((v) => v !== EventType.RUN_STARTED && !RUN_TERMINAL_TYPES.has(v));
    expect(unclassified).toEqual([]);
  });
});

// ── hasUnterminatedRun：recover 反馈区分「回填工具结果」与「补齐 run 终态」──

describe('hasUnterminatedRun', () => {
  it('run/started 之后无终态 → true（无论有没有 dangling）', () => {
    expect(hasUnterminatedRun([ev(EventType.RUN_STARTED, {})])).toBe(true);
    expect(hasUnterminatedRun([
      ev(EventType.RUN_STARTED, {}),
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }),
    ])).toBe(true);
  });

  it('三种终态任一都算收口；之后的 run 重新开一局', () => {
    for (const t of [EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_INTERRUPTED]) {
      expect(hasUnterminatedRun([ev(EventType.RUN_STARTED, {}), ev(t, {})])).toBe(false);
    }
    expect(hasUnterminatedRun([
      ev(EventType.RUN_STARTED, {}),
      ev(EventType.RUN_COMPLETED, {}),
      ev(EventType.RUN_STARTED, {}),
    ])).toBe(true);
  });

  it('无 run 的裸会话 → false（无东西可恢复）', () => {
    expect(hasUnterminatedRun([ev(EventType.SESSION_STARTED, {})])).toBe(false);
    expect(hasUnterminatedRun([])).toBe(false);
  });

  it('与 isRecoverableRun 的「缺终态」分支一致', () => {
    const evs = [ev(EventType.RUN_STARTED, {}), ev(EventType.MODEL_COMPLETED, { content: 'x' })];
    expect(hasUnterminatedRun(evs)).toBe(true);
    expect(isRecoverableRun(evs)).toBe(true);
  });

  it('裸会话（有 dangling 但无 run/started）仍不可恢复——比后端字面语义保守', () => {
    // 这种日志本身畸形：暴露「恢复」入口既不解决问题又给出错误承诺。
    const evs = [
      ev(EventType.SESSION_STARTED, {}),
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }),
    ];
    expect(unpairedToolCallIds(evs).size).toBe(1);
    expect(isRecoverableRun(evs)).toBe(false);
  });
});

// ── recoverDoneMessage：结局不得合并（合并用户就看不出修没修） ──

describe('recoverDoneMessage', () => {
  const none = { stillUnterminated: false, stillDangling: false };

  it('回填了工具结果', () => {
    expect(recoverDoneMessage({ repaired: 2, terminalRepaired: false, ...none }))
      .toBe('已恢复：回填 2 条工具结果');
  });

  it('回填工具结果 + 补齐终态：两条都说', () => {
    expect(recoverDoneMessage({ repaired: 1, terminalRepaired: true, ...none }))
      .toBe('已恢复：回填 1 条工具结果，并补齐 run 终态');
  });

  it('只补齐终态：不能说成「无可修复项」', () => {
    const m = recoverDoneMessage({ repaired: 0, terminalRepaired: true, ...none });
    expect(m).toBe('已恢复：补齐 run 终态');
    expect(m).not.toContain('无可修复项');
  });

  it('回填了但仍缺终态：修复陈述后必须附可重试提示（不能被 repaired>0 吞掉）', () => {
    const m = recoverDoneMessage({
      repaired: 1, terminalRepaired: false, stillUnterminated: true, stillDangling: false,
    });
    expect(m).toContain('回填 1 条工具结果');
    expect(m).toContain('仍缺 run 终态');
    expect(m).toContain('可重试');
  });

  it('原因分开报：终态已补、只剩悬空 tool_call 时不得说「仍缺 run 终态」', () => {
    const m = recoverDoneMessage({
      repaired: 0, terminalRepaired: true, stillUnterminated: false, stillDangling: true,
    });
    expect(m).toContain('仍有未配对工具调用');
    expect(m).not.toContain('仍缺 run 终态');
  });

  it('两个原因都在时报全', () => {
    const m = recoverDoneMessage({
      repaired: 0, terminalRepaired: false, stillUnterminated: true, stillDangling: true,
    });
    expect(m).toContain('仍缺 run 终态且有未配对工具调用');
  });

  it('后端没修完（仍缺终态）→ 如实说可重试，不谎报完整', () => {
    const m = recoverDoneMessage({
      repaired: 0, terminalRepaired: false, stillUnterminated: true, stillDangling: false,
    });
    expect(m).toContain('仍缺 run 终态');
    expect(m).not.toContain('已完整');
  });

  it('事件本已完整 → 无可修复项', () => {
    expect(recoverDoneMessage({ repaired: 0, terminalRepaired: false, ...none }))
      .toContain('已完整');
  });
});

// ── dangling 集合：isRecoverableRun 与「修了几条」统计的共享实现 ──

describe('unpairedToolCallIds', () => {
  it('无配对结果的 tool_call 计入，已配对的剔除', () => {
    const events = [
      ev(EventType.TOOL_CALL, { tool_call_id: 't1', tool_name: 'bash' }),
      ev(EventType.TOOL_RESULT, { tool_call_id: 't1', content: 'ok' }),
      ev(EventType.TOOL_CALL, { tool_call_id: 't2', tool_name: 'bash' }),
      ev(EventType.TOOL_CALL, { tool_call_id: 't3', tool_name: 'bash' }),
      ev(EventType.TOOL_RESULT, { tool_call_id: 't3', content: 'ok' }),
    ];
    expect([...unpairedToolCallIds(events)]).toEqual(['t2']);
  });

  it('恢复补上结果后，before\\after 即「本次修好的条数」', () => {
    const before = [
      ev(EventType.TOOL_CALL, { tool_call_id: 't1' }),
      ev(EventType.TOOL_CALL, { tool_call_id: 't2' }),
    ];
    const after = [...before, ev(EventType.TOOL_RESULT, { tool_call_id: 't1', content: 'ok' })];
    const repaired = [...unpairedToolCallIds(before)].filter(
      (id) => !unpairedToolCallIds(after).has(id),
    );
    expect(repaired).toEqual(['t1']);
  });

  it('无 tool_call → 空集合', () => {
    expect(unpairedToolCallIds([ev(EventType.RUN_STARTED, {})]).size).toBe(0);
  });
});
