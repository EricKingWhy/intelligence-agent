// @vitest-environment jsdom
/** #825（MM-04）：「编辑并重投（supersede）」必须把该轮**已有的附图引用**一起带回。
 *
 *  为什么值得单独钉：后端只按**本请求**的 attachments 重建 `user/message`，supersede
 *  不继承被取代消息的引用（`session/service.py`）；因此编辑保存时不带引用 = 旧轮整段
 *  移除 + 新轮无图 + 字节成孤儿，全程零提示（独立审查 P2）。这条断言正是"以后有人
 *  简化掉第三个实参就会红"的那道闸门。
 *
 *  jsdom + 真实客户端渲染：判据是「点编辑 → 改文 → 点保存」这条交互链落到 `onEditTurn`
 *  的实参上，SSR 车道观察不到（先例：`Composer.attachments.test.tsx`）。
 */

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

import { useDisclosure } from '../lib/disclosure';
import type { Turn } from '../types';
import { TurnView } from './Conversation';

const REF_A = {
  kind: 'image' as const,
  attachment_id: 'sha256:aaa',
  media_type: 'image/png',
  bytes: 12,
  width: 8,
  height: 8,
  name: null,
};
const REF_B = { ...REF_A, attachment_id: 'sha256:bbb' };

/** 一轮「带两张图」的用户消息，且是当前最新一条（`latestEditableSeq` 对齐 ⇒ 可编辑）。 */
const TURN: Turn = {
  step_id: 1,
  user_message: '看这两张图',
  user_attachments: [REF_A, REF_B],
  model: { text: '', status: 'done' },
  segments: [],
  tools: [],
  activities: [],
  status: 'done',
  turn_index: 1,
  user_message_seq: 7,
  started_at: '2026-09-18T00:00:00.000Z',
  completed_at: '2026-09-18T00:00:01.000Z',
};

let container: HTMLDivElement;
let root: Root;
/** `onEditTurn` 的签名（显式命名：契约即断言目标，不用 `ReturnType<typeof vi.fn>`）。 */
type EditTurn = (fromSeq: number, newContent: string, attachmentIds: readonly string[]) => void;

let onEditTurn: Mock<EditTurn>;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onEditTurn = vi.fn<EditTurn>();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function Host({ turn, onEdit }: { turn: Turn; onEdit: EditTurn }) {
  const disclosure = useDisclosure('session-1');
  return createElement(TurnView, {
    turn,
    model: 'model-a',
    density: 'balanced' as const,
    disclosure,
    sessionId: 'session-1',
    latestEditableSeq: 7,
    onEditTurn: onEdit,
  });
}

function click(selector: string): void {
  const el = container.querySelector<HTMLButtonElement>(selector);
  expect(el).not.toBeNull();
  act(() => el?.dispatchEvent(new MouseEvent('click', { bubbles: true })));
}

describe('#825：编辑（supersede）重投递带回复图引用', () => {
  it('保存时把该轮的附件 id 一并交给 onEditTurn', () => {
    act(() => root.render(createElement(Host, { turn: TURN, onEdit: onEditTurn })));
    click('.msg-action-btn');
    click('.msg-edit-actions .msg-edit-btn');
    expect(onEditTurn).toHaveBeenCalledTimes(1);
    expect(onEditTurn).toHaveBeenCalledWith(7, '看这两张图', ['sha256:aaa', 'sha256:bbb']);
  });

  it('纯文本轮次（无附图）仍传空数组——「有值才带键」由发送层执行', () => {
    const plain: Turn = { ...TURN, user_attachments: undefined };
    act(() => root.render(createElement(Host, { turn: plain, onEdit: onEditTurn })));
    click('.msg-action-btn');
    click('.msg-edit-actions .msg-edit-btn');
    expect(onEditTurn).toHaveBeenCalledWith(7, '看这两张图', []);
  });
});
