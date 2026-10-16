/** #1197 项 5A：自动压缩**进行中**的投影事实。
 *
 *  5A UI（"正在压缩上下文…"渲染）按 2026-10-16 20:45 二次证伪裁决 A 已从本票移除。
 *  此处仅保留投影面接线（flag）：`COMPACTION_START` 置 true、`COMPACTION_END` 置 false，
 *  供未来后端语义调整后直接生效。当前无渲染消费者。
 *
 *  ⚠ 已知局限（如实登记）：后端 bracket 三事件是在压缩工作**做完之后**才背靠背落盘
 *  （`builder.py::compact_now` 同一个 `async with guard:` 块），真机上本 flag 的可见
 *  窗口只有两次队列投递之间的微秒级，而 10–60s 的真实等待发生在 START **之前**。
 *  详见 #1252。
 */
import { describe, expect, it } from 'vitest';
import type { AgentEvent } from '../types';
import { EventType } from '../types';
import { applyEvent, initConversation } from './projection';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

describe('applyEvent — 压缩进行中（#1197 项 5A）', () => {
  it('初始态：没有压缩在进行', () => {
    expect(initConversation('s').compaction_in_progress).toBe(false);
  });

  it('COMPACTION_START ⇒ 进行中；COMPACTION_END ⇒ 恢复常态', () => {
    const started = applyEvent(
      initConversation('s'), ev({ type: EventType.COMPACTION_START, seq: 1 }),
    );
    expect(started.compaction_in_progress).toBe(true);
    const ended = applyEvent(started, ev({ type: EventType.COMPACTION_END, seq: 2 }));
    expect(ended.compaction_in_progress).toBe(false);
  });

  it('接线后不再落 unknown_events（此前是显式登记的「未投影」类型）', () => {
    const s = applyEvent(
      initConversation('s'), ev({ type: EventType.COMPACTION_START, seq: 1 }),
    );
    expect(s.unknown_events).toHaveLength(0);
  });

  it('run 终结兜底：bracket 中途夭折也不会把进度条永久钉在屏幕上', () => {
    let s = applyEvent(
      initConversation('s'), ev({ type: EventType.COMPACTION_START, seq: 1 }),
    );
    s = applyEvent(s, ev({ type: EventType.RUN_STARTED, data: { turn_index: 1 }, seq: 2 }));
    s = applyEvent(s, ev({ type: EventType.RUN_FAILED, data: { error: 'boom' }, seq: 3 }));
    expect(s.compaction_in_progress).toBe(false);
  });

  it('引用保持：与压缩无关的事件不改 compaction_in_progress 的值', () => {
    const prev = applyEvent(
      initConversation('s'), ev({ type: EventType.COMPACTION_END, seq: 1 }),
    );
    const next = applyEvent(prev, ev({ type: EventType.MODEL_DELTA, data: { delta: 'x' }, step_id: 1 }));
    expect(next.compaction_in_progress).toBe(false);
  });
});
