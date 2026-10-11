/** #1017：memory/recalled 按轮归位（呈现层归属修复的投影面）。
 *
 * 后端每 run 至多 append 一条 `memory/recalled`（`src/agent_harness/memory/v2/recall.py`
 * 的 context select，只带 `run_id`、不带 `step_id`）。投影把该事件 **verbatim** 挂到
 * 「事件到达时的最后一轮」——即它所属 run 的轮次：append-only 流里
 * `user/message`（建轮）→ `run/started` → `memory/recalled` 的顺序恒成立，
 * 「最后一轮」与「run 归属」在同序重放（live / 历史 / fork）下等价。
 *
 * 不变量 #22：事件流是唯一真相——`turn.recalls` 存 verbatim AgentEvent 引用，
 * 不复制、不改写内容；展开详情仍由 RecallNotice 按 seq/run_id 对账
 * `GET /api/sessions/{id}/memory-recalls`。
 */
import { describe, expect, it } from 'vitest';
import { applyEvent, initConversation } from './projection';
import type { AgentEvent, ConversationState, EventTypeValue } from '../types';

function ev(partial: Partial<AgentEvent> & { type: EventTypeValue | string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

let seq = 0;
/** 一条「纯对话轮」的最小事件流：user/message → run/started → model/completed。 */
function addPlainTurn(s: ConversationState, step: number, runId: string, text: string): ConversationState {
  seq += 1;
  s = applyEvent(s, ev({ type: 'user/message', data: { content: `m-${step}` }, seq, run_id: runId, step_id: step }));
  seq += 1;
  s = applyEvent(s, ev({ type: 'run/started', data: { turn_index: step }, seq, run_id: runId }));
  seq += 1;
  s = applyEvent(s, ev({ type: 'model/completed', data: { content: text }, seq, run_id: runId, step_id: step }));
  return s;
}

function addRecalled(s: ConversationState, runId: string): { state: ConversationState; seq: number } {
  seq += 1;
  return {
    state: applyEvent(s, ev({ type: 'memory/recalled', data: { memories: [] }, seq, run_id: runId })),
    seq,
  };
}

describe('projection — memory/recalled 按轮归位（#1017）', () => {
  it('recalled 事件 verbatim 挂到事件到达时的最后一轮（seq/run_id 原样可对账）', () => {
    let s = initConversation('s-1');
    s = addPlainTurn(s, 1, 'run-1', '答一');
    const { state, seq: recallSeq } = addRecalled(s, 'run-1');
    s = state;

    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].recalls).toHaveLength(1);
    const recall = s.turns[0].recalls![0];
    expect(recall.type).toBe('memory/recalled');
    expect(recall.seq).toBe(recallSeq);
    expect(recall.run_id).toBe('run-1');
    // 事件日志仍是完整真相：recalled 也留在 events（Inspector Timeline 消费）
    expect(s.events.at(-1)?.type).toBe('memory/recalled');
  });

  it('两轮各一条 recalled → 各归各轮（N 条历史 recall 不再并列堆积在尾部）', () => {
    let s = initConversation('s-1');
    s = addPlainTurn(s, 1, 'run-1', '答一');
    s = addRecalled(s, 'run-1').state;
    s = addPlainTurn(s, 2, 'run-2', '答二');
    s = addRecalled(s, 'run-2').state;

    expect(s.turns).toHaveLength(2);
    expect(s.turns[0].recalls).toHaveLength(1);
    expect(s.turns[0].recalls![0].run_id).toBe('run-1');
    expect(s.turns[1].recalls).toHaveLength(1);
    expect(s.turns[1].recalls![0].run_id).toBe('run-2');
  });

  it('无轮次时（defensive）：不新建轮次、不崩溃，事件留在 events 日志', () => {
    const s = addRecalled(initConversation('s-1'), 'run-0').state;
    expect(s.turns).toHaveLength(0);
    expect(s.events.at(-1)?.type).toBe('memory/recalled');
    expect(s.unknown_events).toHaveLength(0); // 已知类型，不进 unknown_events
  });

  it('copy-on-write：recalled 只克隆目标轮，未触及轮引用稳定（memo 前提）', () => {
    let s = initConversation('s-1');
    s = addPlainTurn(s, 1, 'run-1', '答一');
    const first = s.turns[0];
    s = addPlainTurn(s, 2, 'run-2', '答二');
    const second = s.turns[1];
    s = addRecalled(s, 'run-2').state;

    expect(s.turns[0]).toBe(first); // 第 1 轮引用原样
    expect(s.turns[1]).not.toBe(second); // 第 2 轮被克隆替换
    expect(s.turns[1].recalls).toHaveLength(1);
  });

  it('同轮多条 recalled（防御）：都留在该轮，渲染层逐条呈现', () => {
    let s = initConversation('s-1');
    s = addPlainTurn(s, 1, 'run-1', '答一');
    s = addRecalled(s, 'run-1').state;
    s = addRecalled(s, 'run-1').state;

    expect(s.turns[0].recalls).toHaveLength(2);
  });
});
