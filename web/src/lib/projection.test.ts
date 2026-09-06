/** projection.ts 纯函数测试——事件→视图状态机的折叠语义契约。 */

import { describe, expect, it } from 'vitest';
import type { AgentEvent } from '../types';
import { EventType } from '../types';
import { applyEvent, deriveChain, deriveSessionTitle, hasSummaryOverflow, initConversation, projectHistory, summarizeEvent } from './projection';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

describe('initConversation', () => {
  it('初始状态为空对话', () => {
    const s = initConversation('abc');
    expect(s).toEqual({
      session_id: 'abc', turns: [], active_step_id: null, run_status: 'idle', run_cancelled: false,
      compactions: [], reconcile_queue: [], events: [], unknown_events: [],
      model: null, usage_total: null, cost_usd: null, trace_id: null, model_fallback: null,
      seenSeqs: new Set(),
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

  // ── 回归：step_id 字段缺失（undefined）≠ null 的边界 ──
  // 真实后端 user/message 事件不带 step_id 字段（键完全缺失，JSON 解析为 undefined，
  // 不是 null）。resolveStep 用 !== null 判定会让 undefined 漏网返回 undefined，
  // 导致 user/message 与后续 model/completed(step_id=1) 落入不同 turn——模型文本丢失。
  it('user/message 无 step_id 字段（undefined）时与后续 model/completed(step_id=1) 入同一 turn', () => {
    // 模拟真实后端事件：step_id 值为 undefined（运行时等价于键完全缺失，
    // `!= null` 判定一致——strict 判定下 undefined 与 null 行为分叉正是本回归点）
    const userMsg = ev({ type: EventType.USER_MESSAGE, data: { content: '你是谁' }, seq: 1, step_id: undefined });
    const modelCompleted: AgentEvent = { type: EventType.MODEL_COMPLETED, data: { content: '我是 Qwen' }, seq: 3, run_id: null, step_id: 1 };
    let s = applyEvent(initConversation('s'), userMsg);
    s = applyEvent(s, ev({ type: EventType.RUN_STARTED }));
    s = applyEvent(s, modelCompleted);
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].user_message).toBe('你是谁');
    expect(s.turns[0].model.text).toBe('我是 Qwen');
    expect(s.turns[0].step_id).toBe(1);
  });

  // ── 回归：MODEL_COMPLETED 无前置 MODEL_STARTED 时须补 model activity ──
  // 后端某些路径（无工具的纯对话）只发 model/completed 不发 model/started。
  // 此时 turn.activities 没有 model 节点 → Conversation 的 `activities.length > 0`
  // 渲染条件跳过整个 model 输出块 → 模型文本丢失（用户看不到回复）。
  // MODEL_COMPLETED 若发现 turn 还没有任何 model activity，补一个。
  it('MODEL_COMPLETED 无前置 MODEL_STARTED 时补 model segment + activity（渲染入口）', () => {
    const userMsg = ev({ type: EventType.USER_MESSAGE, data: { content: 'hi' }, seq: 1, step_id: undefined });
    const modelCompleted: AgentEvent = { type: EventType.MODEL_COMPLETED, data: { content: 'hello' }, seq: 2, run_id: null, step_id: 1 };
    let s = applyEvent(initConversation('s'), userMsg);
    s = applyEvent(s, ev({ type: EventType.RUN_STARTED }));
    s = applyEvent(s, modelCompleted);
    const turn = s.turns[0];
    expect(turn.segments).toHaveLength(1);
    expect(turn.segments[0].text).toBe('hello');
    expect(turn.segments[0].status).toBe('done');
    expect(turn.activities).toContainEqual({ kind: 'model', index: 0 });
    // turn.model 应与 segments[0] 是同一对象（MODEL_STARTED 的引用对齐不变量）
    expect(turn.model).toBe(turn.segments[0]);
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

  it('applyEvent 是 append-only：既有日志条目永不改写（共享数组只增长）', () => {
    // P0-1 新契约（HANDOFF §6 方案 b）：events 是引用稳定的共享数组，
    // 「不改变既有条目」指条目不 mutate、不重排——不再保证旧 state 的
    // events 长度冻结（旧语义靠每事件 O(N) 克隆换来，是 O(N²) 根因）。
    const e1 = ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' } });
    const e2 = ev({ type: EventType.TOOL_RESULT, data: { tool_call_id: 't1', content: '{"ok":true}' } });
    const afterFirst = applyEvent(initConversation('s1'), e1);
    applyEvent(afterFirst, e2);
    expect(afterFirst.events[0]).toBe(e1); // 既有条目引用不变、不被改写
    expect(afterFirst.events[1]).toBe(e2); // 新条目只追加在尾部
  });

  it('events 日志数组引用跨事件稳定（O(1) 追加契约，消灭 O(N²) 的前提）', () => {
    // 渲染层消费约定（useSession 管线）：只有最新 state 被提交给 React，
    // events 引用稳定不影响 memo 契约（无任何消费者把 events 放进依赖数组）。
    const s1 = applyEvent(initConversation('s1'), ev({ type: EventType.RUN_STARTED }));
    const s2 = applyEvent(s1, ev({ type: EventType.RUN_COMPLETED }));
    expect(s2.events).toBe(s1.events);
    expect(s2.events).toHaveLength(2);
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

describe('applyEvent — 引用稳定性（流式渲染 memo 契约）', () => {
  /** 构造两个已完成的 turn（step 1 / step 2），返回其状态。 */
  function twoDoneTurns() {
    let s = initConversation('s');
    for (const step of [1, 2]) {
      s = applyEvent(s, ev({ type: EventType.RUN_STARTED }));
      s = applyEvent(s, ev({ type: EventType.USER_MESSAGE, data: { content: `q${step}` }, step_id: step }));
      s = applyEvent(s, ev({ type: EventType.MODEL_COMPLETED, data: { content: `a${step}` }, step_id: step }));
    }
    return s;
  }

  it('MODEL_DELTA 只克隆目标 turn——未触及 turn 保持引用', () => {
    const prev = twoDoneTurns();
    const next = applyEvent(prev, ev({ type: EventType.MODEL_DELTA, data: { delta: 'x' }, step_id: 2 }));
    expect(next.turns[0]).toBe(prev.turns[0]);
    expect(next.turns[1]).not.toBe(prev.turns[1]);
    expect(next.turns[1].model.text).toBe('a2x');
  });

  it('不触及 turn 的事件（run/started）保持全部 turn 引用与 turns 数组引用', () => {
    const prev = twoDoneTurns();
    const next = applyEvent(prev, ev({ type: EventType.RUN_STARTED }));
    expect(next.turns).toBe(prev.turns);
    expect(next.turns[0]).toBe(prev.turns[0]);
    expect(next.turns[1]).toBe(prev.turns[1]);
    expect(next.run_status).toBe('running');
  });

  it('RUN_COMPLETED 已 settle 的 turn 保持引用；streaming turn 被替换', () => {
    // run 1 先完成，turn 1/2 拿到 completed_at（settle 完成）
    let s = twoDoneTurns();
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED }));
    // run 2：新 streaming turn
    s = applyEvent(s, ev({ type: EventType.RUN_STARTED }));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 3 }));
    const settledTurn = s.turns[1];
    const streamingTurn = s.turns[2];
    const next = applyEvent(s, ev({ type: EventType.RUN_COMPLETED }));
    expect(next.turns[1]).toBe(settledTurn);
    expect(next.turns[2]).not.toBe(streamingTurn);
    expect(next.turns[2].status).toBe('done');
    expect(next.turns[2].model.status).toBe('done');
  });

  it('TOOL_RESULT 只克隆宿主 turn；被更新的 tool 是新对象，其余 tool 引用不变', () => {
    // turn 1 两个工具，turn 2 一个工具
    let s = initConversation('s');
    s = applyEvent(s, ev({ type: EventType.RUN_STARTED }));
    for (const id of ['t1', 't2']) {
      s = applyEvent(s, ev({ type: EventType.TOOL_CALL, data: { tool_call_id: id, tool_name: 'bash', args: {} }, step_id: 1 }));
    }
    s = applyEvent(s, ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't3', tool_name: 'bash', args: {} }, step_id: 2 }));
    const otherTool = s.turns[0].tools[1]; // t2
    const otherTurn = s.turns[1];
    const next = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true, data: {} }) },
      step_id: 1,
    }));
    expect(next.turns[1]).toBe(otherTurn);
    expect(next.turns[0].tools[1]).toBe(otherTool);
    expect(next.turns[0].tools[0]).not.toBe(s.turns[0].tools[0]);
    expect(next.turns[0].tools[0].status).toBe('success');
  });

  it('ARTIFACT_CREATED 只替换挂载工具所在 turn 与该工具；同 turn 其它工具引用不变', () => {
    let s = initConversation('s');
    s = applyEvent(s, ev({ type: EventType.RUN_STARTED }));
    for (const id of ['t1', 't2']) {
      s = applyEvent(s, ev({ type: EventType.TOOL_CALL, data: { tool_call_id: id, tool_name: 'bash', args: {} }, step_id: 1 }));
    }
    const prev = s;
    const next = applyEvent(s, ev({
      type: EventType.ARTIFACT_CREATED,
      data: { tool_call_id: 't1', artifact_id: 'a1', size: 10, mime_type: 'text/plain', source_tool: 'bash' },
    }));
    expect(next.turns[0]).not.toBe(prev.turns[0]);
    expect(next.turns[0].tools[0]).not.toBe(prev.turns[0].tools[0]);
    expect(next.turns[0].tools[0].artifact?.artifact_id).toBe('a1');
    expect(next.turns[0].tools[1]).toBe(prev.turns[0].tools[1]);
  });

  it('克隆后 turn.model 与 segments[最新 model index] 仍同一对象（别名契约不回退）', () => {
    let s = initConversation('s');
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    const next = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'x' }, step_id: 1 }));
    const turn = next.turns[0];
    const lastModel = [...turn.activities].reverse().find((a) => a.kind === 'model');
    expect(lastModel && turn.segments[lastModel.index]).toBe(turn.model);
  });

  it('compactions / reconcile_queue 未被触及的轮次不重建数组（引用保持）', () => {
    const prev = twoDoneTurns();
    const next = applyEvent(prev, ev({ type: EventType.MODEL_DELTA, data: { delta: 'x' }, step_id: 1 }));
    expect(next.compactions).toBe(prev.compactions);
    expect(next.reconcile_queue).toBe(prev.reconcile_queue);
    expect(next.unknown_events).toBe(prev.unknown_events);
  });
});

// ── 后端 df4f7d8 同步批：工具结果新形状 / 收紧语义识别 ──

describe('applyEvent — df4f7d8 新形状', () => {
  it('bash data.cancelled=true → stopped（中断 ≠ 错误），而非 failed', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' }, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: false, message: 'cancelled', data: { cancelled: true } }) },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].status).toBe('stopped');
    // result 保留原始 data（渲染层读 cancelled 出"已取消"文案）
    expect((s.turns[0].tools[0].result as Record<string, unknown>).cancelled).toBe(true);
  });

  it('bash data.cancelled=false → 正常 ok 判定，不误伤', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'bash' }, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: false, message: 'boom', data: { cancelled: false } }) },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].status).toBe('failed');
  });

  it('MODEL_FAILED 识别为已知终态事件——不落 unknown_events（零产出收紧语义）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.RUN_STARTED }));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_FAILED, data: { error: 'content filtered' }, step_id: 1 }));
    expect(s.unknown_events).toHaveLength(0);
    expect(s.events).toHaveLength(3);
  });

  it('MEMORY_DEGRADED 识别为已知事件（新增 run_id 字段不破坏投影）', () => {
    const s = applyEvent(initConversation('s'), ev({ type: EventType.MEMORY_DEGRADED, data: { reason: 'store unavailable', run_id: 'r1' } }));
    expect(s.unknown_events).toHaveLength(0);
    expect(s.events).toHaveLength(1);
  });

  it('summarizeEvent：MEMORY_DEGRADED / MODEL_FAILED 空摘要（类型标签足够）', () => {
    expect(summarizeEvent(ev({ type: EventType.MEMORY_DEGRADED, data: { reason: 'x' } }))).toBe('');
    expect(summarizeEvent(ev({ type: EventType.MODEL_FAILED }))).toBe('');
  });

  it('空文件 read 成功语义：content:"" + total_lines:0 → success 且 result 保留真值', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'read' }, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true, message: 'read', data: { content: '', total_lines: 0 } }) },
      step_id: 1,
    }));
    const tool = s.turns[0].tools[0];
    expect(tool.status).toBe('success');
    expect((tool.result as Record<string, unknown>).total_lines).toBe(0);
  });
});

describe('applyEvent — recover 合成 tool/result 配对（df4f7d8 无 step_id 形状）', () => {
  it('无 step_id 的 tool/result 按 tool_call_id 全局配对，不造幽灵轮次', () => {
    let s = initConversation('s');
    s = applyEvent(s, ev({ type: EventType.USER_MESSAGE, data: { content: 'hi' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.RUN_STARTED }));
    s = applyEvent(s, ev({ type: EventType.MODEL_COMPLETED, data: { content: 'x' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 'c1', tool_name: 'bash', args: { command: 'echo' } }, step_id: 1 }));
    // 恢复合成形状：step_id 缺失 + 纯文本 content（非 JSON ToolResult）
    s = applyEvent(s, ev({ type: EventType.TOOL_RESULT, data: { tool_call_id: 'c1', content: '工具执行被中断，结果未知' } }));
    expect(s.turns).toHaveLength(1); // 不造幽灵轮次
    expect(s.turns[0].tools[0].status).toBe('failed'); // 非 JSON content → 失败而非 running
  });

  it('TOOL_RESULT 的 step 与 tool/call 落点不一致时回退全局配对（不丢结果）', () => {
    // 条件配对锁定：step 可解析时走快路径（step 定位宿主轮），但快路径
    // 在目标轮找不到该 tool_call_id 时必须回退按 tool_call_id 全局配对——
    // 否则后端 step 不一致形状下结果会被静默丢弃、工具永远停在 running。
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.TOOL_CALL,
      data: { tool_call_id: 't1', tool_name: 'bash', args: {} },
      step_id: 1,
    }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true }) },
      step_id: 2, // 与 tool/call 落点（step 1）不一致
    }));
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].tools[0].status).toBe('success');
  });

  it('TOOL_RESULT 正常同 step 快路径配对（step_id 定位，不依赖全局扫描）', () => {
    let s = applyEvent(initConversation('s'), ev({
      type: EventType.TOOL_CALL,
      data: { tool_call_id: 't1', tool_name: 'bash', args: {} },
      step_id: 1,
    }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true, data: {} }) },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].status).toBe('success');
  });
});

// ── da394a9 同步批：取消态 / diff 归档 marker ──

describe('applyEvent — da394a9 新语义', () => {
  it('run/failed.reason=cancelled → run_cancelled=true（取消 ≠ 失败）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.RUN_STARTED }));
    s = applyEvent(s, ev({ type: EventType.RUN_FAILED, data: { reason: 'cancelled' } }));
    expect(s.run_cancelled).toBe(true);
    expect(s.run_status).toBe('failed'); // 生命周期仍是 failed 终态
  });

  it('run/failed 无 reason（真实失败）→ run_cancelled=false', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.RUN_STARTED }));
    s = applyEvent(s, ev({ type: EventType.RUN_FAILED }));
    expect(s.run_cancelled).toBe(false);
  });

  it('取消后再 completed → run_cancelled 复位（rebuild 不残留旧态）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.RUN_FAILED, data: { reason: 'cancelled' } }));
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED }));
    expect(s.run_cancelled).toBe(false);
  });

  it('diff before 内嵌 inspect_artifact marker → archived=true + artifactId', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'write' }, step_id: 1 }));
    const summary = '文件过大已归档。use inspect_artifact(abc-123) 查看全文';
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true, data: { before: '', after: summary, truncated: true } }) },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].diff).toEqual({
      before: '', after: summary, truncated: true, archived: true, artifactId: 'abc-123',
    });
  });

  it('diff 无 marker → 无 archived 字段（形状不变，零伪造）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.TOOL_CALL, data: { tool_call_id: 't1', tool_name: 'write' }, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_RESULT,
      data: { tool_call_id: 't1', content: JSON.stringify({ ok: true, data: { before: 'a', after: 'b' } }) },
      step_id: 1,
    }));
    expect(s.turns[0].tools[0].diff).toEqual({ before: 'a', after: 'b', truncated: false });
  });
});

// ── Phase 12 白盒透明：tool/failure-guard + model/fallback（ADR-0014）──
describe('Phase 12 — failure-guard / fallback 投影', () => {
  it('TOOL_FAILURE_GUARD soft → 落所在轮 notices，不进 unknown_events', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_FAILURE_GUARD,
      data: { level: 'soft', tool_name: 'bash', fingerprint: 'fp-1', consecutive_failures: 3 },
      step_id: 1,
    }));
    expect(s.turns[0].notices).toEqual([
      { level: 'soft', tool_name: 'bash', consecutive_failures: 3 },
    ]);
    expect(s.unknown_events).toHaveLength(0);
  });

  it('TOOL_FAILURE_GUARD hard → level hard（终止标记语义）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 2 }));
    s = applyEvent(s, ev({
      type: EventType.TOOL_FAILURE_GUARD,
      data: { level: 'hard', tool_name: 'bash', fingerprint: 'fp-2', consecutive_failures: 3 },
      step_id: 2,
    }));
    expect(s.turns[0].notices).toEqual([
      { level: 'hard', tool_name: 'bash', consecutive_failures: 3 },
    ]);
  });

  it('Harness 注入的纠正 user/message（injected_by）标记 turn——不冒充真人输入', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.USER_MESSAGE,
      data: { content: 'bash 连续失败，请改用 PowerShell 重试', injected_by: 'tool_failure_guard', step: 2 },
      step_id: 2,
    }));
    expect(s.turns).toHaveLength(2);
    expect(s.turns[1].injected_by).toBe('tool_failure_guard');
    expect(s.turns[1].user_message).toBe('bash 连续失败，请改用 PowerShell 重试');
  });

  it('真人 user/message 无 injected_by → turn 不带标记', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.USER_MESSAGE, data: { content: 'hi', step: 1 }, step_id: 1,
    }));
    expect(s.turns[0].injected_by).toBeUndefined();
  });

  it('MODEL_FALLBACK → 记录切换 + 后续 model 切到 to_model', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.MODEL_FALLBACK,
      data: { from_model: 'qwen-a', to_model: 'qwen-b', reason: 'ModelStallError' },
      step_id: 1,
    }));
    expect(s.model_fallback).toEqual({ from_model: 'qwen-a', to_model: 'qwen-b', reason: 'ModelStallError' });
    expect(s.model).toBe('qwen-b');
  });

  it('MODEL_FALLBACK 字段缺失不伪造（to_model 缺失 → 不记录切换）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({
      type: EventType.MODEL_FALLBACK,
      data: { from_model: 'qwen-a', reason: 'ModelStallError' },
      step_id: 1,
    }));
    expect(s.model_fallback).toBeNull();
    expect(s.unknown_events).toHaveLength(0); // 已知事件，只是形状不完整
  });

  it('summarizeEvent：guard 行（工具 ×次数 熔断 · 级别）+ fallback 行（from → to · 原因）', () => {
    expect(summarizeEvent(ev({
      type: EventType.TOOL_FAILURE_GUARD,
      data: { level: 'soft', tool_name: 'bash', consecutive_failures: 3 },
    }))).toBe('bash ×3 熔断 · soft');
    expect(summarizeEvent(ev({
      type: EventType.TOOL_FAILURE_GUARD,
      data: { level: 'hard', tool_name: 'bash', consecutive_failures: 3 },
    }))).toBe('bash ×3 熔断 · hard');
    expect(summarizeEvent(ev({
      type: EventType.MODEL_FALLBACK,
      data: { from_model: 'qwen-a', to_model: 'qwen-b', reason: 'ModelStallError' },
    }))).toBe('qwen-a → qwen-b · ModelStallError');
  });
});

describe('Phase 13 — delegation 投影（ADR-0015）', () => {
  const started = (child: string, target = 'research_review', step = 1) => ev({
    type: EventType.AGENT_DELEGATION_STARTED,
    data: { target, task: `为 ${target} 准备任务`, child_session_id: child },
    step_id: step,
  });
  const finished = (child: string, status: 'completed' | 'failed', summary: string, step = 1) => ev({
    type: EventType.AGENT_DELEGATION_FINISHED,
    data: { target: 'research_review', child_session_id: child, status, summary },
    step_id: step,
  });

  it('STARTED → turn.delegations 建节点 running + activities 追加编排项（true event order）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, started('child-1'));
    expect(s.turns[0].delegations).toEqual([
      {
        target: 'research_review',
        task: '为 research_review 准备任务',
        child_session_id: 'child-1',
        status: 'running',
        started_at: expect.any(String),
      },
    ]);
    expect(s.turns[0].activities).toEqual([
      { kind: 'model', index: 0 },
      { kind: 'delegation', child_session_id: 'child-1' },
    ]);
    expect(s.unknown_events).toHaveLength(0);
    // Trace Ladder 视图：deriveChain 吐出编排节点（真事件序）
    const chain = deriveChain(s.turns[0]);
    expect(chain[1]).toEqual({ kind: 'delegation', delegation: s.turns[0].delegations![0] });
  });

  it('FINISHED completed → 回填 status/summary/completed_at；failed → failed', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, started('child-1'));
    s = applyEvent(s, finished('child-1', 'completed', '调研完成：3 篇核心文献'));
    expect(s.turns[0].delegations![0].status).toBe('completed');
    expect(s.turns[0].delegations![0].summary).toBe('调研完成：3 篇核心文献');
    expect(s.turns[0].delegations![0].completed_at).toEqual(expect.any(String));
    s = applyEvent(s, started('child-2', 'coding'));
    s = applyEvent(s, finished('child-2', 'failed', '子代理执行出错'));
    expect(s.turns[0].delegations![1].status).toBe('failed');
  });

  it('并行双委派两对事件（完成顺序落盘）→ 按 child_session_id 各自回填，顺序无关', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, started('child-a', 'research_review'));
    s = applyEvent(s, started('child-b', 'coding'));
    s = applyEvent(s, finished('child-b', 'completed', '编码完成'));
    s = applyEvent(s, finished('child-a', 'failed', '调研超限'));
    const ds = s.turns[0].delegations!;
    expect(ds).toHaveLength(2);
    expect(ds.find((d) => d.child_session_id === 'child-a')).toMatchObject({
      status: 'failed',
      target: 'research_review',
    });
    expect(ds.find((d) => d.child_session_id === 'child-b')).toMatchObject({
      status: 'completed',
      target: 'coding',
    });
  });

  it('重复 STARTED（重放）→ 幂等不重复建节点', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, started('child-1'));
    s = applyEvent(s, started('child-1'));
    expect(s.turns[0].delegations).toHaveLength(1);
    expect(s.turns[0].activities.filter((a) => a.kind === 'delegation')).toHaveLength(1);
  });

  it('FINISHED 先于 STARTED（乱序持久化防御）→ 从 finish 真值建终态节点，不虚构 task', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, finished('child-1', 'completed', '直接到达的结果'));
    const d = s.turns[0].delegations![0];
    expect(d.status).toBe('completed');
    expect(d.summary).toBe('直接到达的结果');
    expect(d.task).toBe('');
    expect(d.started_at).toBeUndefined();
  });

  it('run 结束时仍在 running 的委派 → stopped（中断 ≠ 错误，与 tool 同语义域）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, started('child-1'));
    s = applyEvent(s, ev({ type: EventType.RUN_FAILED }));
    expect(s.turns[0].delegations![0].status).toBe('stopped');
  });

  it('child_session_id 缺失 → 不建节点（不虚构身份），事件仍在 events 日志', () => {
    const s = applyEvent(initConversation('s'), ev({
      type: EventType.AGENT_DELEGATION_STARTED,
      data: { target: 'coding', task: 't' },
      step_id: 1,
    }));
    expect(s.turns).toHaveLength(0);
    expect(s.events).toHaveLength(1);
    expect(s.unknown_events).toHaveLength(0);
  });

  it('summary 溢出指针后缀（后端 #86 冻结契约）→ hasSummaryOverflow 识别，summary 保真不剥离', () => {
    const overflowed = `${'x'.repeat(8192)} [summary 超限已截断至 8192 字符；完整输出见 child session child-1]`;
    expect(hasSummaryOverflow(overflowed)).toBe(true);
    expect(hasSummaryOverflow('正常长度的结果摘要')).toBe(false);
    let s = applyEvent(initConversation('s'), started('child-1'));
    s = applyEvent(s, finished('child-1', 'completed', overflowed));
    expect(s.turns[0].delegations![0].summary).toBe(overflowed);
  });

  it('summarizeEvent：started 行 `委派 → target`；finished 行 `target 完成/失败 · 摘要`', () => {
    expect(summarizeEvent(started('child-1', 'coding'))).toBe('委派 → coding');
    expect(summarizeEvent(finished('child-1', 'completed', '三篇文献综述'))).toBe(
      'research_review 完成 · 三篇文献综述',
    );
    expect(summarizeEvent(finished('child-1', 'failed', ''))).toBe('research_review 失败');
  });

  it('copy-on-write：委派回填只克隆宿主 turn；同会话其它 turn 引用不变', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, started('child-1'));
    s = applyEvent(s, ev({ type: EventType.MODEL_STARTED, step_id: 2 }));
    s = applyEvent(s, ev({ type: EventType.USER_MESSAGE, data: { content: '下一轮', step: 2 }, step_id: 2 }));
    const turn1Before = s.turns[0];
    const turn2Before = s.turns[1];
    s = applyEvent(s, finished('child-1', 'completed', 'done'));
    expect(s.turns[0]).not.toBe(turn1Before);
    expect(s.turns[1]).toBe(turn2Before);
  });
});

describe('T1 — seq 幂等去重 + 帧校验（#94，spec 02 §6/§14）', () => {
  it('重复 seq 的持久事件整帧丢弃（at-least-once 不重复投影）', () => {
    const first = ev({ type: EventType.USER_MESSAGE, data: { content: 'hi', step: 1 }, seq: 5 });
    const s1 = applyEvent(initConversation('s'), first);
    expect(s1.events).toHaveLength(1);
    const s2 = applyEvent(s1, { ...first });
    expect(s2.events).toHaveLength(1);
    expect(s2.turns).toHaveLength(1);
    expect(s2.turns[0].user_message).toBe('hi');
  });

  it('重复 seq 不重复进 events 日志，也不产生 unknown_events', () => {
    const e = ev({ type: EventType.MODEL_COMPLETED, data: { content: 'x', step: 1 }, seq: 2 });
    const s = applyEvent(applyEvent(initConversation('s'), e), { ...e });
    expect(s.events).toHaveLength(1);
    expect(s.unknown_events).toHaveLength(0);
  });

  it('null seq（model/delta 等流式帧）永不去重', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.MODEL_STARTED, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'a' }, step_id: 1 }));
    s = applyEvent(s, ev({ type: EventType.MODEL_DELTA, data: { delta: 'b' }, step_id: 1 }));
    expect(s.turns[0].model.text).toBe('ab');
  });

  it('未见过的回跳 seq（乱序补达）不丢弃——精确重复才幂等，乱序小窗 DEFER', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.RUN_STARTED, seq: 5 }));
    s = applyEvent(s, ev({ type: EventType.RUN_COMPLETED, seq: 3 }));
    expect(s.events).toHaveLength(2);
    expect(s.run_status).toBe('completed');
  });

  it('畸形帧（type 非字符串）隔离进 unknown_events——不崩、不投影、verbatim 留档', () => {
    const s = applyEvent(
      initConversation('s'),
      { data: {}, seq: 1, run_id: null, step_id: null, type: 123 } as unknown as AgentEvent,
    );
    expect(s.unknown_events).toHaveLength(1);
    expect(s.unknown_events[0].type).toBe('malformed/event');
    expect(s.turns).toHaveLength(0);
    expect(s.events).toHaveLength(1);
  });

  it('缺 data 的帧不崩溃：校验层归一化为 {}（原 data.content 直取的 TypeError 向量）', () => {
    const s = applyEvent(
      initConversation('s'),
      { type: EventType.USER_MESSAGE, seq: null, run_id: null, step_id: null } as unknown as AgentEvent,
    );
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].user_message).toBe('');
  });
});

describe('T2 — reasoning 事件投影（#95，契约 C1，spec 03 §5/§7）', () => {
  const rStarted = (step: number, blockId: string | undefined, extra: Partial<AgentEvent> = {}) =>
    ev({
      type: 'reasoning/started',
      data: blockId === undefined ? {} : { block_id: blockId },
      step_id: step,
      ...extra,
    });
  const rDelta = (step: number, blockId: string | undefined, delta: string, extra: Partial<AgentEvent> = {}) =>
    ev({
      type: 'reasoning/delta',
      data: { delta, ...(blockId === undefined ? {} : { block_id: blockId }) },
      step_id: step,
      ...extra,
    });
  const rTerminal = (type: string, step: number, blockId: string | undefined, extra: Partial<AgentEvent> = {}) =>
    ev({
      type,
      data: blockId === undefined ? {} : { block_id: blockId },
      step_id: step,
      ...extra,
    });

  it('started→delta→completed 聚合为一个 block，activities 记录真实顺序', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rStarted(1, 'b1'));
    s = applyEvent(s, rDelta(1, 'b1', '想'));
    s = applyEvent(s, rDelta(1, 'b1', '一下'));
    s = applyEvent(s, rTerminal('reasoning/completed', 1, 'b1'));
    const t = s.turns[0];
    expect(t.reasoningById?.['b1']).toMatchObject({ blockId: 'b1', text: '想一下', status: 'completed' });
    expect(t.activities).toContainEqual({ kind: 'reasoning', blockId: 'b1' });
  });

  it('S2 分段语义：reasoning → tool → reasoning 是兄弟节点，deriveChain 保序', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rStarted(1, 'b1'));
    s = applyEvent(s, rTerminal('reasoning/completed', 1, 'b1'));
    s = applyEvent(s, ev({ type: EventType.TOOL_CALL, data: { name: 'bash', tool_call_id: 'tc1' }, step_id: 1 }));
    s = applyEvent(s, rStarted(1, 'b2'));
    s = applyEvent(s, rTerminal('reasoning/completed', 1, 'b2'));
    const kinds = deriveChain(s.turns[0]).map((n) => n.kind);
    expect(kinds).toEqual(['reasoning', 'tool', 'reasoning']);
  });

  it('completed 不可变：终态后的迟到 delta 不改文本（spec 02 §8.1）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rStarted(1, 'b1'));
    s = applyEvent(s, rTerminal('reasoning/completed', 1, 'b1'));
    s = applyEvent(s, rDelta(1, 'b1', '迟到'));
    expect(s.turns[0].reasoningById?.['b1']).toMatchObject({ text: '', status: 'completed' });
  });

  it('interrupted 保留已聚合文本并终结（PRD §16.4：部分内容不擦除）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rStarted(1, 'b1'));
    s = applyEvent(s, rDelta(1, 'b1', 'partial '));
    s = applyEvent(s, rDelta(1, 'b1', 'text'));
    s = applyEvent(s, rTerminal('reasoning/interrupted', 1, 'b1'));
    expect(s.turns[0].reasoningById?.['b1']).toMatchObject({ text: 'partial text', status: 'interrupted' });
  });

  it('visibility=internal 不建用户可见块（spec 02 §15 硬边界），events 日志仍 verbatim', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rStarted(1, 'x', { data: { block_id: 'x', visibility: 'internal' } }));
    s = applyEvent(s, rDelta(1, 'x', 'secret', { data: { delta: 'secret', block_id: 'x', visibility: 'internal' } }));
    s = applyEvent(s, rTerminal('reasoning/completed', 1, 'x', { data: { block_id: 'x', visibility: 'internal' } }));
    expect(s.turns[0].reasoningById?.['x']).toBeUndefined();
    expect(s.turns[0].activities.some((a) => a.kind === 'reasoning')).toBe(false);
    expect(s.events).toHaveLength(4);
  });

  it('缺 block_id 的 delta 落到本 turn 最近一个 streaming block（宽松契约降级）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rStarted(1, 'b1'));
    s = applyEvent(s, rDelta(1, undefined, 'fallthrough'));
    expect(s.turns[0].reasoningById?.['b1']).toMatchObject({ text: 'fallthrough', status: 'streaming' });
  });

  it('无 started 直接 delta：合成 key 建块（delta 是事实，零伪造建块）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rDelta(1, undefined, 'orphan'));
    const blocks = s.turns[0].reasoningById ?? {};
    const ids = Object.keys(blocks);
    expect(ids).toHaveLength(1);
    expect(Object.values(blocks)[0]).toMatchObject({ text: 'orphan', status: 'streaming' });
    expect(s.turns[0].activities.some((a) => a.kind === 'reasoning')).toBe(true);
  });

  it('source 缺省为 model；data.source=agent 显式可辨（S1 双来源）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, rStarted(1, 'b1'));
    s = applyEvent(s, rStarted(1, 'b2', { data: { block_id: 'b2', source: 'agent' } }));
    expect(s.turns[0].reasoningById?.['b1']).toMatchObject({ source: 'model' });
    expect(s.turns[0].reasoningById?.['b2']).toMatchObject({ source: 'agent' });
  });

  it('历史重放同构：projectHistory === 逐帧 applyEvent（持久事件恒带 time）', () => {
    const events = [
      ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 }, seq: 1, time: '2026-09-06T00:00:01Z' }),
      rStarted(1, 'b1', { seq: 2, time: '2026-09-06T00:00:02Z' }),
      rDelta(1, 'b1', 'a', { seq: 3, time: '2026-09-06T00:00:03Z' }),
      rTerminal('reasoning/completed', 1, 'b1', { seq: 4, time: '2026-09-06T00:00:04Z' }),
    ];
    let live = initConversation('s');
    for (const e of events) live = applyEvent(live, e);
    expect(projectHistory('s', events)).toEqual(live);
  });

  it('summarizeEvent：终态有摘要、delta 记字符数（同 model/delta 惯例）', () => {
    expect(summarizeEvent(rTerminal('reasoning/completed', 1, 'b1'))).toContain('思考');
    expect(summarizeEvent(rTerminal('reasoning/interrupted', 1, 'b1'))).toContain('中断');
    expect(summarizeEvent(rStarted(1, 'b1'))).toContain('思考');
    expect(summarizeEvent(rDelta(1, 'b1', 'xyz'))).toBe('+3 字符');
  });
});

describe('T3 — tool/output_delta 流式输出（#96，契约 C2，spec 03 §9.3）', () => {
  const callEvent = (id: string, step = 1) =>
    ev({ type: EventType.TOOL_CALL, data: { tool_call_id: id, tool_name: 'bash', args: { command: 'ls' } }, step_id: step });
  const outDelta = (id: string, channel: string, delta: string, extra: Partial<AgentEvent> = {}) =>
    ev({ type: 'tool/output_delta', data: { tool_call_id: id, channel, delta }, step_id: 1, ...extra });
  const resultEvent = (id: string, step = 1, extra: Partial<AgentEvent> = {}) =>
    ev({ type: EventType.TOOL_RESULT, data: { tool_call_id: id, content: JSON.stringify({ ok: true, data: { exit_code: 0 } }) }, step_id: step, ...extra });

  it('stdout delta 逐段累积到运行中工具的 output 缓冲', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, callEvent('tc1'));
    s = applyEvent(s, outDelta('tc1', 'stdout', 'RUN'));
    s = applyEvent(s, outDelta('tc1', 'stdout', ' tests\n'));
    expect(s.turns[0].tools[0].output).toEqual([{ channel: 'stdout', text: 'RUN tests\n' }]);
    expect(s.turns[0].tools[0].status).toBe('running');
  });

  it('stderr 独立通道保真——双通道不串（验收：channel 徽标/分色）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, callEvent('tc1'));
    s = applyEvent(s, outDelta('tc1', 'stdout', 'out\n'));
    s = applyEvent(s, outDelta('tc1', 'stderr', 'warn\n'));
    expect(s.turns[0].tools[0].output).toEqual([
      { channel: 'stdout', text: 'out\n' },
      { channel: 'stderr', text: 'warn\n' },
    ]);
  });

  it('result 到达：终态校准且流式 chunks 保留（流式内容不丢弃）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, callEvent('tc1'));
    s = applyEvent(s, outDelta('tc1', 'stdout', 'partial'));
    s = applyEvent(s, resultEvent('tc1'));
    const tool = s.turns[0].tools[0];
    expect(tool.status).toBe('success');
    expect(tool.output).toEqual([{ channel: 'stdout', text: 'partial' }]);
  });

  it('未知 tool_call_id 的 output_delta 不伪造工具，事件仍 verbatim 在 events', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, outDelta('ghost', 'stdout', 'x'));
    expect(s.turns[0].tools).toHaveLength(0);
    expect(s.events).toHaveLength(2);
    expect(s.unknown_events).toHaveLength(0);
  });

  it('channel 非 stdout/stderr 的帧丢弃（契约 C2 严格两通道，不误标）', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, callEvent('tc1'));
    s = applyEvent(s, outDelta('tc1', 'bogus', 'x'));
    expect(s.turns[0].tools[0].output).toBeUndefined();
  });

  it('慢路径配对：delta 无 step_id 也能按 tool_call_id 全局定位宿主轮', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, callEvent('tc1'));
    s = applyEvent(s, ev({ type: 'tool/output_delta', data: { tool_call_id: 'tc1', channel: 'stdout', delta: 'x' }, step_id: null }));
    expect(s.turns[0].tools[0].output).toEqual([{ channel: 'stdout', text: 'x' }]);
  });

  it('历史重放同构：output_delta + result 重放与逐帧应用一致', () => {
    const events = [
      ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 }, seq: 1, time: '2026-09-06T00:00:01Z' }),
      { ...callEvent('tc1'), seq: 2, time: '2026-09-06T00:00:02Z' },
      { ...outDelta('tc1', 'stdout', 'a\n'), seq: 3, time: '2026-09-06T00:00:03Z' },
      { ...outDelta('tc1', 'stderr', 'b\n'), seq: 4, time: '2026-09-06T00:00:04Z' },
      { ...resultEvent('tc1'), seq: 5, time: '2026-09-06T00:00:05Z' },
    ];
    let live = initConversation('s');
    for (const e of events) live = applyEvent(live, e);
    expect(projectHistory('s', events)).toEqual(live);
  });

  it('summarizeEvent：output_delta 同 delta 惯例（+N 字符）', () => {
    expect(summarizeEvent(outDelta('tc1', 'stdout', 'xyz'))).toBe('+3 字符');
  });

  it('交替通道流有界收缩：chunk 数超上限触发前半合并，文本零丢失', () => {
    let s = applyEvent(initConversation('s'), ev({ type: EventType.USER_MESSAGE, data: { content: 'q', step: 1 } }));
    s = applyEvent(s, callEvent('tc1'));
    for (let i = 0; i < 1200; i++) {
      s = applyEvent(s, outDelta('tc1', i % 2 ? 'stderr' : 'stdout', `L${i}\n`));
    }
    const chunks = s.turns[0].tools[0].output ?? [];
    expect(chunks.length).toBeLessThanOrEqual(512);
    const total = chunks.reduce((n, c) => n + c.text.length, 0);
    expect(total).toBeGreaterThan(0);
    expect(chunks[0].text).toContain('L0\n');
    expect(chunks[chunks.length - 1].text).toContain('L1199\n');
  });
});
