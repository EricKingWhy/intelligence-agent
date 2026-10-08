/** #825（MM-04）：`user/message.data.attachments` 的投影（AC5「刷新/重入后仍在」的上游事实）。

 *  钉住两件事：
 *  1. 附件是 `content` 的平行字段，逐帧覆盖（与 user_message 同语义）；
 *  2. 容错粒度是**逐条**（对齐后端 `attachments/projection.py::parse_image_refs`）——
 *     一行坏引用只损失那一行，不能让整个会话视图 brick。
 */

import { describe, expect, it } from 'vitest';
import type { AgentEvent } from '../types';
import { EventType } from '../types';
import { applyEvent, initConversation } from './projection';

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

describe('projectUserMessage — 附件引用（#825）', () => {
  it('无 attachments 时 turn.user_attachments 不设（= 纯文本轮，渲染层不渲染图片区）', () => {
    const s = applyEvent(
      initConversation('s1'),
      ev({ type: EventType.USER_MESSAGE, data: { content: '看这张图', step: 1 } }),
    );
    expect(s.turns[0].user_message).toBe('看这张图');
    expect(s.turns[0].user_attachments).toBeUndefined();
  });

  it('合法引用落到该轮的 user_attachments（顺序 = 附图顺序）', () => {
    const s = applyEvent(
      initConversation('s1'),
      ev({
        type: EventType.USER_MESSAGE,
        data: { content: '两张', step: 1, attachments: [ref('sha256:aa'), ref('sha256:bb')] },
      }),
    );
    expect(s.turns[0].user_attachments?.map((a) => a.attachment_id)).toEqual([
      'sha256:aa',
      'sha256:bb',
    ]);
    expect(s.turns[0].user_attachments?.[0]).toEqual({
      kind: 'image',
      attachment_id: 'sha256:aa',
      media_type: 'image/png',
      bytes: 1234,
      width: 640,
      height: 480,
      name: null,
    });
  });

  it('坏引用逐条跳过，好引用照常进（不因一行坏数据丢掉整轮图片）', () => {
    const s = applyEvent(
      initConversation('s1'),
      ev({
        type: EventType.USER_MESSAGE,
        data: {
          content: '混合',
          step: 1,
          attachments: [
            null,
            'not-an-object',
            { kind: 'file', attachment_id: 'sha256:skip' },
            { kind: 'image', attachment_id: '', media_type: 'image/png', bytes: 1, width: 1, height: 1 },
            { kind: 'image', attachment_id: 'sha256:ok', media_type: 'image/png', bytes: 1, width: 1, height: 1 },
            { kind: 'image', attachment_id: 'sha256:nan', media_type: 'image/png', bytes: 1, width: Number.NaN, height: 1 },
          ],
        },
      }),
    );
    expect(s.turns[0].user_attachments?.map((a) => a.attachment_id)).toEqual(['sha256:ok']);
  });

  it('attachments 存在但全为坏形状 → 不设字段（不是空数组：渲染层只判存在性）', () => {
    const s = applyEvent(
      initConversation('s1'),
      ev({ type: EventType.USER_MESSAGE, data: { content: 'x', step: 1, attachments: ['junk'] } }),
    );
    expect(s.turns[0].user_attachments).toBeUndefined();
  });

  it('带 name 的引用保留 name（后端未来持久化 name 时无需改投影）', () => {
    const s = applyEvent(
      initConversation('s1'),
      ev({
        type: EventType.USER_MESSAGE,
        data: { content: 'x', step: 1, attachments: [{ ...ref('sha256:cc'), name: 'shot.png' }] },
      }),
    );
    expect(s.turns[0].user_attachments?.[0].name).toBe('shot.png');
  });
});
