// @vitest-environment jsdom
/** #1283 P2-2：排队项「编辑 → 保存」的失败保留契约。
 *
 *  保存走 `POST /messages {queue_id}`，与 steer/flush 共用待答 409 闸门；组件此前
 *  在同一帧就 `setEditingId(null)`，409 一回来用户改好的文本随编辑态一起消失。
 *  这里用真 DOM 驱动点击与输入，钉住「未受理（显式 `false`）⇒ 保留编辑态与草稿」。
 */
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createElement } from 'react';
import { Composer } from './Composer';
import type { UndeliveredInput } from '../types';

const NOOP = () => {};

const QUEUE: UndeliveredInput[] = [
  { kind: 'queue', id: 'q-1', content: '原始排队内容', seq: 1, created_at: '' },
];

let container: HTMLDivElement | null = null;
let root: Root | null = null;

/** React 的受控 input 只认原生 setter 触发的 input 事件。 */
function setInputValue(el: HTMLInputElement, value: string): void {
  const setter = Object.getOwnPropertyDescriptor(
    HTMLInputElement.prototype, 'value',
  )?.set;
  setter?.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
}

async function mount(onEditItem: (item: UndeliveredInput, content: string) => void | boolean | Promise<void | boolean>): Promise<HTMLDivElement> {
  const el = document.createElement('div');
  document.body.appendChild(el);
  container = el;
  root = createRoot(el);
  await act(async () => {
    root!.render(createElement(Composer, {
      streaming: false,
      onSubmit: NOOP,
      onCancel: NOOP,
      onEnsureSession: async () => null,
      undelivered: QUEUE,
      onEditItem,
      onSteerItem: NOOP,
      onFlush: NOOP,
    }));
  });
  return el;
}

/** 重渲染：模拟「编辑态已打开后，待答请求才从事件流推来」的迟到锁。 */
async function rerender(onEditItem: (item: UndeliveredInput, content: string) => void | boolean | Promise<void | boolean>): Promise<void> {
  await act(async () => {
    root!.render(createElement(Composer, {
      streaming: false,
      onSubmit: NOOP,
      onCancel: NOOP,
      onEnsureSession: async () => null,
      undelivered: QUEUE,
      onEditItem,
      onSteerItem: NOOP,
      onFlush: NOOP,
      askUserInputPending: true,
    }));
  });
}

function button(el: HTMLElement, label: string): HTMLButtonElement {
  const found = el.querySelector<HTMLButtonElement>(`button[aria-label="${label}"]`);
  if (!found) throw new Error(`button not found: ${label}`);
  return found;
}

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: NOOP, removeEventListener: NOOP,
    addListener: NOOP, removeListener: NOOP, dispatchEvent: () => false,
  }));
  vi.stubGlobal('ResizeObserver', class { observe = NOOP; unobserve = NOOP; disconnect = NOOP; });
  vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', {
    status: 200, headers: { 'content-type': 'application/json' },
  })));
});

afterEach(async () => {
  if (root) await act(async () => { root!.unmount(); });
  if (container) container.remove();
  document.body.innerHTML = '';
  root = null;
  container = null;
  vi.unstubAllGlobals();
});

describe('#1283 P2-2 — 编辑保存被拒时保留编辑态与草稿', () => {
  it('onEditItem 返回 false（409/未受理）⇒ 编辑框与改好的文本都还在', async () => {
    const onEditItem = vi.fn(async () => false);
    const el = await mount(onEditItem);

    await act(async () => { button(el, '编辑排队消息').click(); });
    const input = el.querySelector<HTMLInputElement>('input.queue-item-edit');
    expect(input).not.toBeNull();

    await act(async () => { setInputValue(input!, '改好的文本'); });
    await act(async () => { button(el, '保存排队消息').click(); });
    // 等异步回执落定。
    await act(async () => { await Promise.resolve(); });

    expect(onEditItem).toHaveBeenCalledTimes(1);
    expect(onEditItem).toHaveBeenCalledWith(QUEUE[0], '改好的文本');
    const stillEditing = el.querySelector<HTMLInputElement>('input.queue-item-edit');
    expect(stillEditing).not.toBeNull();
    expect(stillEditing!.value).toBe('改好的文本');
  });

  it('onEditItem 返回 true（已受理）⇒ 编辑态关闭（旧行为不回归）', async () => {
    const onEditItem = vi.fn(async () => true);
    const el = await mount(onEditItem);

    await act(async () => { button(el, '编辑排队消息').click(); });
    await act(async () => { button(el, '保存排队消息').click(); });
    await act(async () => { await Promise.resolve(); });

    expect(onEditItem).toHaveBeenCalledTimes(1);
    expect(el.querySelector('input.queue-item-edit')).toBeNull();
  });

  it('onEditItem 返回 undefined（旧签名 = 已受理）⇒ 编辑态关闭', async () => {
    const onEditItem = vi.fn(() => undefined);
    const el = await mount(onEditItem);

    await act(async () => { button(el, '编辑排队消息').click(); });
    await act(async () => { button(el, '保存排队消息').click(); });
    await act(async () => { await Promise.resolve(); });

    expect(onEditItem).toHaveBeenCalledTimes(1);
    expect(el.querySelector('input.queue-item-edit')).toBeNull();
  });

  it('onEditItem 抛错 ⇒ 按未受理处理：保留编辑态与草稿，并留可观察痕迹', async () => {
    const boom = new Error('network down');
    const onEditItem = vi.fn(async () => { throw boom; });
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});
    const el = await mount(onEditItem);

    await act(async () => { button(el, '编辑排队消息').click(); });
    const input = el.querySelector<HTMLInputElement>('input.queue-item-edit');
    await act(async () => { setInputValue(input!, '抛错后仍要留下'); });
    await act(async () => { button(el, '保存排队消息').click(); });
    await act(async () => { await Promise.resolve(); });

    // #1283 复审 P4：restore 放 finally——前面任一 expect 失败时若不恢复，
    // 本文件后续用例的 console.error 会被持续静默，掩盖真实错误日志。
    try {
      const stillEditing = el.querySelector<HTMLInputElement>('input.queue-item-edit');
      expect(stillEditing).not.toBeNull();
      expect(stillEditing!.value).toBe('抛错后仍要留下');
      expect(spy).toHaveBeenCalled();
    } finally {
      spy.mockRestore();
    }
  });

  it('锁在编辑态打开之后才到 ⇒ Enter 也不提交（不发必然 409 的请求），草稿保留', async () => {
    const onEditItem = vi.fn(async () => true);
    const el = await mount(onEditItem);

    await act(async () => { button(el, '编辑排队消息').click(); });
    const input = el.querySelector<HTMLInputElement>('input.queue-item-edit');
    expect(input).not.toBeNull();
    await act(async () => { setInputValue(input!, '锁后改好的文本'); });

    // 锁前基线（#1283 复审 P4）：若保存按钮被无条件禁用，下面的断言会失去鉴别力。
    expect(button(el, '保存排队消息').disabled).toBe(false);

    // 迟到锁：编辑态已经打开，待答请求才从事件流推来。
    await rerender(onEditItem);

    const save = button(el, '保存排队消息');
    expect(save.disabled).toBe(true);
    expect(save.title).toBe('请先回答当前提问');
    // Enter 是静默 no-op（不弹任何东西），原因只能由输入框自身给出。
    expect(
      el.querySelector<HTMLInputElement>('input.queue-item-edit')!.title,
    ).toBe('请先回答当前提问');

    await act(async () => {
      el.querySelector<HTMLInputElement>('input.queue-item-edit')!.dispatchEvent(
        new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }),
      );
    });
    await act(async () => { await Promise.resolve(); });

    expect(onEditItem).not.toHaveBeenCalled();
    const stillEditing = el.querySelector<HTMLInputElement>('input.queue-item-edit');
    expect(stillEditing).not.toBeNull();
    expect(stillEditing!.value).toBe('锁后改好的文本');
  });
});
