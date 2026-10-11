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
 *
 * 夹具说明：`ev()` 恒写 `step_id: null` 键（镜像 SSE 帧超集形态，types.ts 信封
 * 注释——线上帧键可缺省，null 与键缺失在 `!= null` 判定下等价）；「后端 recalled
 * 不带 step_id」指该字段不为本轮归属提供信息，不是键在帧里不存在。
 */
import { describe, expect, it } from 'vitest';
import { applyEvent, initConversation } from './projection';
import type { AgentEvent, ConversationState, EventTypeValue } from '../types';

function ev(partial: Partial<AgentEvent> & { type: EventTypeValue | string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

/** 事件流构建器：seq 每个 state 独立计数（审查 P4——不用模块级计数器跨用例累加）。 */
function stream() {
  let seq = 0;
  let s: ConversationState = initConversation('s-1');
  return {
    state: () => s,
    userMessage(step: number, runId: string): void {
      s = applyEvent(s, ev({ type: 'user/message', data: { content: `m-${step}` }, seq: ++seq, run_id: runId, step_id: step }));
    },
    runStarted(step: number, runId: string): void {
      s = applyEvent(s, ev({ type: 'run/started', data: { turn_index: step }, seq: ++seq, run_id: runId }));
    },
    recalled(runId: string): number {
      const recallSeq = ++seq;
      s = applyEvent(s, ev({ type: 'memory/recalled', data: { memories: [] }, seq: recallSeq, run_id: runId }));
      return recallSeq;
    },
    modelCompleted(step: number, runId: string, text: string): void {
      s = applyEvent(s, ev({ type: 'model/completed', data: { content: text }, seq: ++seq, run_id: runId, step_id: step }));
    },
  };
}

describe('projection — memory/recalled 按轮归位（#1017）', () => {
  it('recalled 事件 verbatim 挂到事件到达时的最后一轮（seq/run_id 原样可对账）', () => {
    const t = stream();
    t.userMessage(1, 'run-1');
    t.runStarted(1, 'run-1');
    const recallSeq = t.recalled('run-1');

    const s = t.state();
    expect(s.turns).toHaveLength(1);
    expect(s.turns[0].recalls).toHaveLength(1);
    const recall = s.turns[0].recalls![0];
    expect(recall.type).toBe('memory/recalled');
    expect(recall.seq).toBe(recallSeq);
    expect(recall.run_id).toBe('run-1');
    // 事件日志仍是完整真相：recalled 也留在 events（Inspector Timeline 消费）
    expect(s.events.at(-1)?.type).toBe('memory/recalled');
  });

  it('run/started 给最后一轮盖 run_id 归属戳（recalled 校验锚）', () => {
    const t = stream();
    t.userMessage(1, 'run-1');
    t.runStarted(1, 'run-1');
    expect(t.state().turns[0].run_id).toBe('run-1');
  });

  it('两轮各一条 recalled → 各归各轮（N 条历史 recall 不再并列堆积在尾部）', () => {
    const t = stream();
    t.userMessage(1, 'run-1');
    t.runStarted(1, 'run-1');
    t.recalled('run-1');
    t.modelCompleted(1, 'run-1', '答一');
    t.userMessage(2, 'run-2');
    t.runStarted(2, 'run-2');
    t.recalled('run-2');
    t.modelCompleted(2, 'run-2', '答二');

    const s = t.state();
    expect(s.turns).toHaveLength(2);
    expect(s.turns[0].recalls).toHaveLength(1);
    expect(s.turns[0].recalls![0].run_id).toBe('run-1');
    expect(s.turns[1].recalls).toHaveLength(1);
    expect(s.turns[1].recalls![0].run_id).toBe('run-2');
  });

  it('无轮次时（defensive）：不新建轮次、不崩溃，事件留在 events 日志', () => {
    const t = stream();
    t.recalled('run-0');
    const s = t.state();
    expect(s.turns).toHaveLength(0);
    expect(s.events.at(-1)?.type).toBe('memory/recalled');
    expect(s.unknown_events).toHaveLength(0); // 已知类型，不进 unknown_events
  });

  it('copy-on-write：recalled 只克隆目标轮，未触及轮引用稳定（memo 前提）', () => {
    const t = stream();
    t.userMessage(1, 'run-1');
    t.runStarted(1, 'run-1');
    t.modelCompleted(1, 'run-1', '答一');
    const first = t.state().turns[0];
    t.userMessage(2, 'run-2');
    t.runStarted(2, 'run-2');
    t.modelCompleted(2, 'run-2', '答二');
    const second = t.state().turns[1];
    t.recalled('run-2');

    const s = t.state();
    expect(s.turns[0]).toBe(first); // 第 1 轮引用原样
    expect(s.turns[1]).not.toBe(second); // 第 2 轮被克隆替换
    expect(s.turns[1].recalls).toHaveLength(1);
  });

  it('同轮多条 recalled（防御）：都留在该轮，渲染层逐条呈现', () => {
    const t = stream();
    t.userMessage(1, 'run-1');
    t.runStarted(1, 'run-1');
    t.recalled('run-1');
    t.recalled('run-1');
    expect(t.state().turns[0].recalls).toHaveLength(2);
  });

  it('审查 P3：recalled 的 run_id 与宿主轮归属戳不等 → 弃挂（乱序回跳防错挂）', () => {
    const t = stream();
    t.userMessage(1, 'run-1');
    t.runStarted(1, 'run-1');
    // 乱序回跳：上一轮 run 的 recalled 在下一轮建轮后才到达（ADR-0016 DEFER 场景）
    t.userMessage(2, 'run-2');
    t.runStarted(2, 'run-2');
    t.recalled('run-late');

    const s = t.state();
    expect(s.turns[0].recalls).toBeUndefined(); // 不静默挂进错轮
    expect(s.turns[1].recalls).toBeUndefined();
    expect(s.events.at(-1)?.type).toBe('memory/recalled'); // 事件仍在日志（真相无损）
  });

  it('审查 P3：任一侧 run_id 缺失 → 无法证伪，按序归位（旧数据不回归）', () => {
    const t = stream();
    t.userMessage(1, '');
    // run/started 信封不带 run_id：不盖章；recalled 同样不带 → 按序归位
    const evNoRun = { data: {}, seq: 0, run_id: null, step_id: null } as AgentEvent;
    let s = applyEvent(t.state(), { ...evNoRun, seq: 99, type: 'run/started' } as AgentEvent);
    s = applyEvent(s, { ...evNoRun, seq: 100, type: 'memory/recalled' } as AgentEvent);
    expect(s.turns[0].recalls).toHaveLength(1); // 归属照常
    expect(s.turns[0].run_id).toBeUndefined(); // 无戳可校验
  });
});
