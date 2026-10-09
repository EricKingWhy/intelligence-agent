/** #825（MM-04）：待发送输入（队列 / steer）里的附件引用投影。
 *
 *  为什么必须钉：`message/queued` / `steer/requested` 事件里带着与该条输入一起提交的
 *  附件引用（后端 `_enqueue` 把 `user_input_metadata` 原样写进事件）。前端若不解析它，
 *  队列条的「立即 / 编辑」重投递就不带引用，后端按本请求重建 `user/message` ⇒ 图静默
 *  消失（独立审查 F4）。另钉 `GET /queue` 补齐路径：该端点**不下发**附件，补齐时不得
 *  把事件流已告知的引用抹掉。
 */

import { describe, expect, it } from 'vitest';
import type { AgentEvent, ConversationState } from '../types';
import { EventType } from '../types';
import { applyEvent, initConversation, restoreUndeliveredFromQueue } from './projection';

function ev(partial: Partial<AgentEvent> & { type: string }): AgentEvent {
  return { data: {}, seq: null, run_id: null, step_id: null, ...partial };
}

const ref = (id: string) => ({
  kind: 'image',
  attachment_id: id,
  media_type: 'image/png',
  bytes: 1234,
  width: 640,
  height: 480,
});

function queued(state: ConversationState, data: Record<string, unknown>): ConversationState {
  return applyEvent(state, ev({ type: EventType.MESSAGE_QUEUED, data }));
}

describe('待发送输入的附件引用（#825）', () => {
  it('message/queued 带 attachments → 落到该条的 attachments（顺序不变）', () => {
    const s = queued(initConversation('s1'), {
      queue_id: 'q1',
      content: '排队里的图',
      attachments: [ref('sha256:aaa'), ref('sha256:bbb')],
    });
    expect(s.undelivered.map((u) => [u.kind, u.id])).toEqual([['queue', 'q1']]);
    expect(s.undelivered[0].attachments?.map((a) => a.attachment_id)).toEqual([
      'sha256:aaa',
      'sha256:bbb',
    ]);
  });

  it('steer/requested 同样解析 attachments', () => {
    const s = applyEvent(
      initConversation('s1'),
      ev({
        type: EventType.STEER_REQUESTED,
        data: { steer_id: 'st1', content: '插一句话', attachments: [ref('sha256:ccc')] },
      }),
    );
    expect(s.undelivered[0].kind).toBe('steer');
    expect(s.undelivered[0].attachments?.map((a) => a.attachment_id)).toEqual(['sha256:ccc']);
  });

  it('没有 attachments 字段 → 不设（= 该条本来就没有图，不是空数组）', () => {
    const s = queued(initConversation('s1'), { queue_id: 'q1', content: '纯文本' });
    expect(s.undelivered[0].attachments).toBeUndefined();
  });

  it('坏引用逐条跳过（一行坏数据不得让队列条整片失效）', () => {
    const s = queued(initConversation('s1'), {
      queue_id: 'q1',
      content: '混着坏数据',
      attachments: [{ kind: 'image', attachment_id: 42 }, ref('sha256:ok')],
    });
    expect(s.undelivered[0].attachments?.map((a) => a.attachment_id)).toEqual(['sha256:ok']);
  });

  it('GET /queue 补齐（不下发附件）时按 id 保留事件流已知的引用', () => {
    const s = queued(initConversation('s1'), {
      queue_id: 'q1',
      content: '排队里的图',
      attachments: [ref('sha256:aaa')],
    });
    // 补齐里还有一条事件流没见过的（重连后新增，或事件已滚出窗口）——如实为空。
    restoreUndeliveredFromQueue(s, {
      items: [
        { queue_id: 'q1', content: '排队里的图', created_at: '2026-10-08T00:00:00Z' },
        { queue_id: 'q2', content: '别的', created_at: '2026-10-08T00:00:01Z' },
      ],
      steers: [],
    });
    expect(s.undelivered.map((u) => [u.id, u.attachments?.length ?? 0])).toEqual([
      ['q1', 1],
      ['q2', 0],
    ]);
    expect(s.undelivered[0].attachments?.[0].attachment_id).toBe('sha256:aaa');
  });
});
