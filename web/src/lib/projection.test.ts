/** projection.ts 纯函数测试——事件→视图状态机的折叠语义契约。 */

import { describe, expect, it } from 'vitest';
import type { AgentEvent } from '../types';
import { EventType } from '../types';
import { applyEvent, deriveChain, deriveSessionTitle, initConversation, projectHistory, summarizeEvent } from './projection';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

describe('initConversation', () => {
  it('初始状态为空对话', () => {
    const s = initConversation('abc');
    expect(s).toEqual({
      session_id: 'abc', turns: [], active_step_id: null, run_status: 'idle',
      compactions: [], reconcile_queue: [], events: [], unknown_events: [],
      model: null, usage_total: null, cost_usd: null, trace_id: null,
    });
  });
});

describe('applyEvent — 折叠语义', () => {
  it('USER_MESSAGE 按 data.step 创建轮次并写入用户消息', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.USER_MESSAGE,
      data: { content: 'hi', step: 2 },
      step_id: 7,
    }));
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].step_id).toBe(2);
    expect(s.turns[0].user_message).toBe('hi');
  });

  it('RUN_STARTED → running；RUN_COMPLETED → completed 并折叠 streaming 轮次', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.RUN_STARTED }));
    expect(s.run_status).toBe('running');
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED }));
    expect(s.run_status).toBe('completed');
    expect(s.active_step_id).toBeNull();
    expect(s.turns[0].status).toBe('done');
    expect(s.turns[0].model.status).toBe('done');
  });

  it('RUN_FAILED → failed 且 streaming 轮次标记 failed', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 3 }));
    s = applyEvent(s, ev({ type: EventType.RUN_FAILED }));
    expect(s.run_status).toBe('failed');
    expect(s.turns[0].status).toBe('failed');
  });

  it('MODEL_DELTA 累积文本并保持 streaming', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'Hel' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'lo' }, step_id: 1 }));
    expect(s.turns[0].model.text).toBe('Hello');
    expect(s.turns[0].model.status).toBe('streaming');
  });

  it('MODEL_COMPLETED 的 content 覆盖累积 delta', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'partial' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_COMPLETED, data: { content: 'final' }, step_id: 1 }));
    expect(s.turns[0].model.text).toBe('final');
    expect(s.turns[0].model.status).toBe('done');
  });

  it('TOOL_CALL 创建 running 工具；重复 tool_call_id 不重复创建', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    const call = { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: { command: 'ls' } }, step_id: 1 };
    s = applyEvent(s, ev(call));
    s = applyEvent(s, ev(call));
    expect(s.turns[0].tools).toHaveLength(1);
    expect(s.turns[0].tools[0].status).toBe('running');
  });

  it('TOOL_RESULT 双重编码 content 解析：ok→success，diff 字段从 data 顶层提取', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'write' }, step_id: 1 }));
    const toolResult = JSON.stringify({
      ok: true,
      message: 'written',
      data: { before: '', after: 'hello', truncated: false },
    });
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: toolResult },
      step_id: 1,
    }));
    const tool = s.turns[0].tools[0];
    expect(tool.status).toBe('success');
    expect(tool.diff).toEqual({ before: '', after: 'hello', truncated: false });
  });

  it('TOOL_RESULT ok=false → failed', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' }, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: false, message: 'boom' }) },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].status).toBe('failed');
  });

  it('时间真值：durable 事件的 time 优先于客户端时钟', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'write' }, step_id: 1, time: '2026-09-04T01:02:03.000Z' }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true }) },
      step_id: 1,
      time: '2026-09-04T01:02:04.500Z',
    }));
    expect(s.turns[0].tools[0].started_at).toBe('2026-09-04T01:02:03.000Z');
    expect(s.turns[0].tools[0].completed_at).toBe('2026-09-04T01:02:04.500Z');
  });

  it('UnknownSurface 兜底：未知事件记录到 unknown_events 而非静默丢弃（冻结决策第 69 行）', () => {
    const s = applyEvent(initConversation('s'), ev({ type: 'future/thing', data: { x: 1 } }));
    expect(s.turns).toHaveLength(0);
    expect(s.unknown_events).toHaveLength(1);
    expect(s.unknown_events[0].type).toBe('future/thing');
    expect(s.events).toHaveLength(1);
  });

  it('已知生命周期事件 SESSION_STARTED / SESSION_RESUMED 不进 unknown_events', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.SESSION_STARTED }));
    s = applyEvent(s, ev({ type: EventType.SESSION_RESUMED }));
    expect(s.unknown_events).toHaveLength(0);
    expect(s.events).toHaveLength(2);
  });

  it('工具四态 stopped：run 结束时仍在 running 的工具被标记 stopped（DSH interrupted ≠ error）', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.TOOL_CALL,
      data: { tool_call_id: 't1', tool_name: 'bash' },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].status).toBe('running');
    // 未收到 TOOL_RESULT 就 RUN_FAILED → 工具被中断
    s = applyEvent(s, ev({ type: EventType.RUN_FAILED }));
    expect(s.turns[0].tools[0].status).toBe('stopped');
  });

  it('已完成（成功或失败）的工具在 run 结束时不被改成 stopped', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.TOOL_CALL,
      data: { tool_call_id: 't1', tool_name: 'bash' },
      step_id: 1,
    }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true }) },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].status).toBe('success');
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED }));
    // success 保持，不被 finalizeRun 改动
    expect(s.turns[0].tools[0].status).toBe('success');
  });
});

describe('deriveSessionTitle — Session Model E 轮首条用户消息投影', () => {
  it('返回首条 user/message 的 content', () => {
    const events = [
      ev({ type: EventType.SESSION_STARTED }),
      ev({ type: EventType.USER_MESSAGE, data: { content: '写一个 FizzBuzz', step: 1 } }),
      ev({ type: EventType.MODEL_COMPLETED, data: { content: 'ok', step: 1 } }),
    ];
    expect(deriveSessionTitle(events)).toBe('写一个 FizzBuzz');
  });

  it('无 user/message 时返回空串（调用方回退到短 ID，不伪造）', () => {
    const events = [
      ev({ type: EventType.SESSION_STARTED }),
      ev({ type: EventType.RUN_STARTED }),
    ];
    expect(deriveSessionTitle(events)).toBe('');
  });

  it('空白 content 视为无标题（trim 后为空）', () => {
    const events = [
      ev({ type: EventType.USER_MESSAGE, data: { content: '   ', step: 1 } }),
    ];
    expect(deriveSessionTitle(events)).toBe('');
  });

  it('多条 user/message 只取首条（不随后续轮次更新）', () => {
    const events = [
      ev({ type: EventType.USER_MESSAGE, data: { content: '第一句', step: 1 } }),
      ev({ type: EventType.USER_MESSAGE, data: { content: '第二句', step: 2 } }),
    ];
    expect(deriveSessionTitle(events)).toBe('第一句');
  });
});

describe('applyEvent — resolveStep 边界契约', () => {
  // 这组测试钉死 resolveStep 的语义优先级链（commit 3663ab8 统一后的形态），
  // 防止未来重构再次引入 6 处 step 解析变体时回退。
  // 优先级：data.step → step_id → active_step_id → turns.length+1

  it('data.step 优先于 step_id：当两者冲突时采纳 data.step（显式信号胜出）', () => {
    let s = initConversation('s');
    s = applyEvent(s, ev({
      type: EventType.MODEL_STARTED,
      data: { step: 5 },      // 显式声明 step=5
      step_id: 9,             // 持久化携带 step_id=9（冲突）
    }));
    // turn 应落在 step=5，active_step_id 也应是 5
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].step_id).toBe(5);
    expect(s.active_step_id).toBe(5);
  });

  it('无 data.step 时回退到 step_id', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.MODEL_STARTED,
      step_id: 3,
    }));
    expect(s.turns[0].step_id).toBe(3);
    expect(s.active_step_id).toBe(3);
  });

  it('MODEL_STARTED 无 data.step 且无 step_id、已有 turn 时走 turns.length+1（非旧的 1）', () => {
    // 构造一个已有 1 个 turn 的状态（active_step_id 已被前一个 MODEL_STARTED 设过）
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.USER_MESSAGE,
      data: { content: 'first', step: 1 },
    }));
    expect(s.turns).toHaveLength(1);
    // 此时无任何 active step（USER_MESSAGE 不设 active_step_id），无 step_id 的
    // MODEL_STARTED 应回退到 turns.length+1 = 2，而不是旧行为的 1。
    s = applyEvent(s, ev({
      type: EventType.MODEL_STARTED,
      // 故意不带 data.step 也不带 step_id
    }));
    expect(s.turns).toHaveLength(2);
    expect(s.turns[1].step_id).toBe(2);
    expect(s.active_step_id).toBe(2);
  });

  it('MODEL_DELTA 无 step_id 时跟随 active_step_id', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.MODEL_STARTED,
      data: { step: 4 },
    }));
    s = applyEvent(s, ev({
      type: EventType.MODEL_DELTA,
      data: { delta: 'x' },
      // 不带 step_id，应跟随 active_step_id=4
    }));
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].step_id).toBe(4);
    expect(s.turns[0].model.text).toBe('x');
  });
});

describe('applyEvent — Phase 5 新事件投影', () => {
  // 这组测试覆盖 Phase 5 新增的 3 个事件类型（ARTIFACT_CREATED /
  // CONTEXT_COMPACTED / OPERATION_RECONCILE_REQUIRED）。
  // MODEL_FAILED 是死常量（后端声明但无构造点），暂不投影（走 default 忽略）。

  it('ARTIFACT_CREATED 把 artifact ref 挂到产生它的 ToolCall 上', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.TOOL_CALL,
      data: { tool_call_id: 't1', tool_name: 'bash', args: { command: 'cat big.log' } },
      step_id: 1,
    }));
    s = applyEvent(s, ev({
      type: EventType.ARTIFACT_CREATED,
      data: {
        artifact_id: 'art-abc123',
        session_id: 's',
        source_tool: 'bash',
        tool_call_id: 't1',
        size: 1048576,
        mime_type: 'text/plain',
      },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].artifact).toEqual({
      artifact_id: 'art-abc123',
      size: 1048576,
      mime_type: 'text/plain',
      source_tool: 'bash',
    });
  });

  it('ARTIFACT_CREATED 找不到对应 tool_call_id 时安全忽略（不崩溃）', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.ARTIFACT_CREATED,
      data: {
        artifact_id: 'art-x',
        session_id: 's',
        source_tool: 'bash',
        tool_call_id: 'nonexistent',
        size: 100,
        mime_type: 'text/plain',
      },
    }));
    expect(s.turns).toHaveLength(0);
  });

  it('CONTEXT_COMPACTED 记录到 conversation.compactions（Inspector 数据源）', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.CONTEXT_COMPACTED,
      data: {
        compacted_turn_count: 5,
        summary_message_count: 1,
        token_estimate: 2048,
        fallback_used: false,
      },
      time: '2026-09-04T10:00:00.000Z',
    }));
    expect(s.compactions).toHaveLength(1);
    expect(s.compactions[0]).toEqual({
      compacted_turn_count: 5,
      summary_message_count: 1,
      token_estimate: 2048,
      fallback_used: false,
      time: '2026-09-04T10:00:00.000Z',
    });
  });

  it('OPERATION_RECONCILE_REQUIRED 入队到 reconcile_queue', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.OPERATION_RECONCILE_REQUIRED,
      data: {
        tool_call_id: 't9',
        tool_name: 'bash',
        args_identity: 'rm -rf /tmp/x',
        state: 'NEED_RECONCILE',
      },
      time: '2026-09-04T11:00:00.000Z',
    }));
    expect(s.reconcile_queue).toHaveLength(1);
    expect(s.reconcile_queue[0]).toEqual({
      tool_call_id: 't9',
      tool_name: 'bash',
      args_identity: 'rm -rf /tmp/x',
      state: 'NEED_RECONCILE',
      time: '2026-09-04T11:00:00.000Z',
    });
  });

  it('多次 CONTEXT_COMPACTED 累积（不全量覆盖）', () => {
    let s = initConversation('s');
    s = applyEvent(s, ev({
      type: EventType.CONTEXT_COMPACTED,
      data: { compacted_turn_count: 3, summary_message_count: 1, token_estimate: 1024, fallback_used: false },
    }));
    s = applyEvent(s, ev({
      type: EventType.CONTEXT_COMPACTED,
      data: { compacted_turn_count: 2, summary_message_count: 1, token_estimate: 2048, fallback_used: true },
    }));
    expect(s.compactions).toHaveLength(2);
    expect(s.compactions[1].fallback_used).toBe(true);
  });

  it('initConversation 新字段初始化为空数组', () => {
    const s = initConversation('s');
    expect(s.compactions).toEqual([]);
    expect(s.reconcile_queue).toEqual([]);
  });
});

describe('projectHistory — 从持久事件重建', () => {
  it('重建结果与逐事件 apply 一致，且不携带流式 delta', () => {
    const history: AgentEvent[] = [
      { type: EventType.SESSION_STARTED, data: {}, seq: 0, run_id: null, step_id: null },
      { type: EventType.USER_MESSAGE, data: { content: 'task' }, seq: 1, run_id: null, step_id: 1 },
      { type: EventType.MODEL_COMPLETED, data: { content: 'done' }, seq: 2, run_id: null, step_id: 1 },
      { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: {} }, seq: 3, run_id: null, step_id: 1 },
      { type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: JSON.stringify({ ok: true }) }, seq: 4, run_id: null, step_id: 1 },
      { type: EventType.RUN_COMPLETED, data: {}, seq: 5, run_id: null, step_id: null },
    ];
    const s = projectHistory('abc', history);
    expect(s.session_id).toBe('abc');
    expect(s.run_status).toBe('completed');
    expect(s.turns[0].user_message).toBe('task');
    expect(s.turns[0].model.text).toBe('done');
    expect(s.turns[0].tools[0].status).toBe('success');
  });
});

describe('applyEvent — 执行链投影（Phase 3）', () => {
  it('同 step 多轮 model burst 各存一段，turn.model 指向最新段', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1, time: '2026-09-04T00:00:00.000Z' }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'first' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_COMPLETED, data: { content: 'first done' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1, time: '2026-09-04T00:00:05.000Z' }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'second' }, step_id: 1 }));
    expect(s.turns[0].segments).toHaveLength(2);
    expect(s.turns[0].segments[0]).toEqual({ text: 'first done', status: 'done' });
    expect(s.turns[0].segments[1]).toEqual({ text: 'second', status: 'streaming' });
    expect(s.turns[0].model).toEqual({ text: 'second', status: 'streaming' });
  });

  it('activities 记录事件真序：model → tool → model', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'a' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'b' }, step_id: 1 }));
    const t = s.turns[0];
    expect(t.activities).toEqual([
      { kind: 'model', index: 0 },
      { kind: 'tool', tool_call_id: 't1' },
      { kind: 'model', index: 1 },
    ]);
  });

  it('重复 MODEL_STARTED（无 delta）不追加空段；重复 TOOL_CALL 不重复记 activity', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    const call = { type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' }, step_id: 1 };
    s = applyEvent(s, ev(call));
    s = applyEvent(s, ev(call));
    expect(s.turns[0].segments).toHaveLength(1);
    expect(s.turns[0].activities.filter((a) => a.kind === 'tool')).toHaveLength(1);
  });

  it('turn 时间真值：started_at 来自首个触碰事件，completed_at 来自 finalizeRun', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1, time: '2026-09-04T00:00:01.000Z' }));
    expect(s.turns[0].started_at).toBe('2026-09-04T00:00:01.000Z');
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED, time: '2026-09-04T00:00:09.500Z' }));
    expect(s.turns[0].completed_at).toBe('2026-09-04T00:00:09.500Z');
  });

  it('deriveChain：activities 顺序展开为 model/tool 混合链', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'hi' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash', args: {} }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    const chain = deriveChain(s.turns[0]);
    expect(chain.map((n) => n.kind)).toEqual(['model', 'tool', 'model']);
    expect(chain[0].kind === 'model' && chain[0].segment.text).toBe('hi');
    expect(chain[1].kind === 'tool' && chain[1].tool.tool_call_id).toBe('t1');
  });

  it('clone 后 model↔segments 别名重新对齐：后续 delta 落在最新段且同步可见', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'a' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.TOOL_RESULT, data: { tool_call_id: 'x', content: 'done' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    // 这次 applyEvent 的 clone 必须重新对齐 model 与 segments[last index]，
    // 否则此 delta 只改 turn.model，deriveChain 读到的 segment 仍是空串
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'b' }, step_id: 1 }));
    const t = s.turns[0];
    expect(t.segments[1].text).toBe('b');
    expect(t.model).toBe(t.segments[1]);
  });
});

describe('applyEvent — Inspector Timeline 事件日志（Phase 5）', () => {
  it('每个事件原样追加到 conversation.events（真相源，零过滤）', () => {
    const e1 = ev({ type: EventType.RUN_STARTED });
    const e2 = ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' } });
    const state = [e1, e2].reduce(applyEvent, initConversation('s1'));
    expect(state.events).toEqual([e1, e2]);
  });

  it('applyEvent 是纯追加：后续事件不改变既有日志条目', () => {
    const e1 = ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' } });
    const e2 = ev({ type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: '{"ok":true}' } });
    const afterFirst = applyEvent(initConversation('s1'), e1);
    const snapshot = [...afterFirst.events];
    applyEvent(afterFirst, e2);
    expect(afterFirst.events).toEqual(snapshot);
  });

  it('projectHistory 重建的 events 与输入事件序列一致', () => {
    const events = [
      ev({ type: EventType.RUN_STARTED }),
      ev({ type: EventType.USER_MESSAGE, data: { content: 'hi', step: 1 } }),
      ev({ type: EventType.MODEL_COMPLETED, data: { content: 'ok', step: 1 } }),
      ev({ type: EventType.RUN_COMPLETED }),
    ];
    const state = projectHistory('s1', events);
    expect(state.events).toEqual(events);
    expect(state.events).toHaveLength(4);
  });

  it('stream-only 事件（model/delta）也进日志——Timeline 显示折叠后的计数视图', () => {
    const state = [
      ev({ type: EventType.MODEL_DELTA, data: { delta: 'a', step: 1 } }),
      ev({ type: EventType.MODEL_DELTA, data: { delta: 'b', step: 1 } }),
    ].reduce(applyEvent, initConversation('s1'));
    expect(state.events).toHaveLength(2);
  });
});

describe('applyEvent — Run 观测字段投影（后端 Gap 1/2）', () => {
  it('MODEL_COMPLETED 捕获 model 名与 usage（可选字段）', () => {
    const s = applyEvent(
      initConversation('s'),
      ev({
        type: EventType.MODEL_COMPLETED,
        data: { content: 'ok', model: 'qwen-plus-0911', usage: { prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 } },
        step_id: 1,
      }),
    );
    expect(s.model).toBe('qwen-plus-0911');
    expect(s.usage_total).toEqual({ prompt_tokens: 100, completion_tokens: 50, total_tokens: 150 });
  });

  it('多次 MODEL_COMPLETED 的 usage 累加（run/completed 到达前的运行中视图）', () => {
    let s = initConversation('s');
    for (const total of [150, 200]) {
      s = applyEvent(s, ev({
        type: EventType.MODEL_COMPLETED,
        data: { content: 'x', model: 'm', usage: { prompt_tokens: total, completion_tokens: 0, total_tokens: total } },
        step_id: 1,
      }));
    }
    expect(s.usage_total).toEqual({ prompt_tokens: 350, completion_tokens: 0, total_tokens: 350 });
  });

  it('RUN_COMPLETED 的 usage_total 是权威聚合——覆盖前端累计值，并捕获 cost_usd / trace_id', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.MODEL_COMPLETED,
      data: { content: 'x', usage: { prompt_tokens: 999, completion_tokens: 999, total_tokens: 1998 } },
      step_id: 1,
    }));
    s = applyEvent(s, ev({
      type: EventType.RUN_COMPLETED,
      data: {
        usage_total: { prompt_tokens: 1234, completion_tokens: 567, total_tokens: 1801 },
        cost_usd: 0.0024,
        trace_id: 'lf-abc',
      },
    }));
    expect(s.usage_total).toEqual({ prompt_tokens: 1234, completion_tokens: 567, total_tokens: 1801 });
    expect(s.cost_usd).toBe(0.0024);
    expect(s.trace_id).toBe('lf-abc');
  });

  it('RUN_COMPLETED 未携带观测字段 → 保留前端累计 usage，cost/trace 保持 null（不伪造 0）', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.MODEL_COMPLETED,
      data: { content: 'x', usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } },
      step_id: 1,
    }));
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED, data: {} }));
    expect(s.usage_total).toEqual({ prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 });
    expect(s.cost_usd).toBeNull();
    expect(s.trace_id).toBeNull();
  });

  it('畸形 usage（字段缺失/类型错误/非有限数）整体按 null 处理——绝不部分伪造', () => {
    for (const bad of [
      { prompt_tokens: 1 },                                  // 缺字段
      { prompt_tokens: '1', completion_tokens: 2, total_tokens: 3 }, // 类型错
      { prompt_tokens: 1, completion_tokens: 2, total_tokens: Number.NaN }, // 非有限
    ]) {
      const s = applyEvent(initConversation('s'), ev({
        type: EventType.MODEL_COMPLETED,
        data: { content: 'x', usage: bad },
        step_id: 1,
      }));
      expect(s.usage_total).toBeNull();
    }
  });

  it('cost_usd 为显式 null 或非有限数 → null（费率表未定义的预期降级）', () => {
    for (const cost of [null, Number.NaN]) {
      const s = applyEvent(initConversation('s'), ev({
        type: EventType.RUN_COMPLETED,
        data: { cost_usd: cost },
      }));
      expect(s.cost_usd).toBeNull();
    }
  });

  it('RUN_FAILED 不捕获 run 级观测字段（契约只定义在 run/completed），已累计 usage 保留', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.MODEL_COMPLETED,
      data: { content: 'x', usage: { prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 } },
      step_id: 1,
    }));
    s = applyEvent(s, ev({ type: EventType.RUN_FAILED, data: { usage_total: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 }, cost_usd: 1 } }));
    expect(s.usage_total).toEqual({ prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 });
    expect(s.cost_usd).toBeNull();
  });

  it('无观测字段的 MODEL_COMPLETED 不改动 model/usage（保持 null）', () => {
    const s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_COMPLETED, data: { content: 'x' }, step_id: 1 }));
    expect(s.model).toBeNull();
    expect(s.usage_total).toBeNull();
  });
});

describe('summarizeEvent — 事件单行摘要（观测字段 + UnknownSurface 边界）', () => {
  it('MODEL_COMPLETED：观测字段存在 → 「模型 · N tok」；缺失 → 回退内容长度', () => {
    expect(summarizeEvent(ev({
      type: EventType.MODEL_COMPLETED,
      data: { content: 'hello', model: 'qwen-plus-0911', usage: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 1801 } },
    }))).toBe('qwen-plus-0911 · 1801 tok');
    expect(summarizeEvent(ev({ type: EventType.MODEL_COMPLETED, data: { content: 'hello' } }))).toBe('5 字符');
  });

  it('RUN_COMPLETED：聚合用量/成本摘要；全缺失 → 空摘要', () => {
    expect(summarizeEvent(ev({
      type: EventType.RUN_COMPLETED,
      data: { usage_total: { prompt_tokens: 1, completion_tokens: 2, total_tokens: 1801 }, cost_usd: 0.0024 },
    }))).toBe('1801 tok · $0.0024');
    expect(summarizeEvent(ev({ type: EventType.RUN_COMPLETED, data: {} }))).toBe('');
  });

  it('已知生命周期事件返回空摘要——「未知事件」兜底只留给真正未知的类型', () => {
    for (const type of [
      EventType.RUN_STARTED, EventType.RUN_FAILED, EventType.SESSION_STARTED,
      EventType.SESSION_RESUMED, EventType.MODEL_STARTED, EventType.MODEL_FAILED,
    ]) {
      expect(summarizeEvent(ev({ type }))).toBe('');
    }
  });
});
