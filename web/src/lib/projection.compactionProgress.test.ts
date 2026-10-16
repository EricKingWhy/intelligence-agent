/** #1197 项 5A：自动压缩**进行中**的投影事实。
 *
 *  票面契约：`COMPACTION_START` 已发出、`COMPACTION_END` 未到 ⇒ 前端必须显示
 *  「正在压缩上下文…」，不许静默；`COMPACTION_END` 到达后自动消失恢复常态。
 *  本文件钉**投影面**（flag），组件面钉 `Conversation.compactionProgress.test.tsx`。
 *
 *  ⚠ 已知局限（如实登记）：后端 bracket 三事件是在压缩工作**做完之后**才背靠背落盘
 *  （`builder.py::compact_now` 同一个 `async with guard:` 块），真机上本 flag 的可见
 *  窗口只有两次队列投递之间的微秒级，而 10–60s 的真实等待发生在 START **之前**。
 *  票面「不许改 COMPACTION_START/END 后端事件语义」把修法限制在前端，故此处按票面
 *  字面实现，并把局限留在报告里。
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
