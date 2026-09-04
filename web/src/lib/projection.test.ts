/** projection.ts 纯函数测试——事件→视图状态机的折叠语义契约。 */

import { describe, expect, it } from 'vitest';
import type { AgentEvent } from '../types';
import { EventType } from '../types';
import { applyEvent, initConversation, projectHistory } from './projection';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

describe('initConversation', () => {
  it('初始状态为空对话', () => {
    const s = initConversation('abc');
    expect(s).toEqual({ session_id: 'abc', turns: [], active_step_id: null, run_status: 'idle' });
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

  it('未知事件类型被忽略（前向兼容）', () => {
    const s = applyEvent(initConversation('s'), ev({ type: 'future/thing' }));
    expect(s.turns).toHaveLength(0);
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
