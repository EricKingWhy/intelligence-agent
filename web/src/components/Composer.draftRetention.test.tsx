// @vitest-environment jsdom
/** #1029（[Web Composer] 待裁决会话发送必丢文本）：**ack 之前不清空、失败留住草稿**。
 *
 * 缺陷形态（修复前）：`submit()` 在提交瞬间无条件执行 `setValue('')` +
 * `attachments.clearSent(...)`，完全不等待发送结果，源码注释亦自陈「清空发生在提交
 * 瞬间、失败不回填」。于是**任何**发送失败（断代理、429、待裁决 409……）都把用户唯一
 * 不可再生的资产无声丢掉——`GET /queue` 为空、事件日志无痕，事后完全不可恢复。
 *
 * 本文件按票面 AC3 的两面锁住修复：
 *  - 正常会话：**投递被受理之后**才清空（ack 前不清）；
 *  - 模拟失败：文本保留 + 附件留在栏里（失败后一键重发的凭据，AC2）。
 * 另加两条同族纪律（同为"别吞用户输入"）：
 *  - 提交后用户又打了字 ⇒ 只清本次提交拥有那段前缀，新输入绝不被抹掉；
 *  - 失败回填只在输入框为空时发生，绝不覆盖用户的新草稿。
 *
 * 手法与 `Composer.sendGate.test.tsx` 同源：只 mock 传输层（XHR），`onSubmit`/`onSteer`
 * 由测试驱动其落定时机——这正是本票的关键变量（"何时算已受理"）。
 */

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, uploadAttachment: vi.fn() };
});

import { uploadAttachment, type AttachmentUploadReceipt } from '../lib/api';
import { Composer } from './Composer';

type SubmitArgs = [task: string, rememberAsProceduralRule: boolean, attachmentIds?: string[]];
type SubmitResult = Promise<boolean> | boolean | void;

let container: HTMLDivElement;
let root: Root;
let onSubmit: Mock<(...args: SubmitArgs) => SubmitResult>;
let onSteer: Mock<(...args: SubmitArgs) => SubmitResult>;
/** 驱动"投递落定"：`resolve` 让它落成已受理/未受理。 */
let settleSubmit: ((accepted: boolean) => void) | null;
let submitted: Promise<boolean> | null;

const receipt = (id: string): AttachmentUploadReceipt => ({
  attachment_id: id,
  media_type: 'image/png',
  bytes: 12,
  width: 8,
  height: 8,
});

function imageFile(name: string, size = 12): File {
  const bytes = Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a);
  const file = new File([bytes], name, { type: 'image/png' });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

/** 一次"悬挂的投递"：`onSubmit`/`onSteer` 返回的 promise 由测试决定何时落定。 */
function hangingDelivery(): Promise<boolean> {
  submitted = new Promise<boolean>((resolve) => { settleSubmit = resolve; });
  return submitted;
}

let resolveUpload: ((value: AttachmentUploadReceipt) => void) | null;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  settleSubmit = null;
  submitted = null;
  resolveUpload = null;
  onSubmit = vi.fn<(...args: SubmitArgs) => SubmitResult>();
  onSteer = vi.fn<(...args: SubmitArgs) => SubmitResult>();
  vi.mocked(uploadAttachment).mockImplementation(() => ({
    promise: new Promise<AttachmentUploadReceipt>((resolve) => { resolveUpload = resolve; }),
    abort: vi.fn(),
  }));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.mocked(uploadAttachment).mockReset();
});

function paint(): void {
  act(() => {
    root.render(createElement(Composer, {
      streaming: false,
      onSubmit,
      onSteer,
      onCancel: () => {},
      sessionId: 'sess-1',
      onEnsureSession: async () => null,
      models: [],
      selectedModel: null,
    }));
  });
}

/** 带 `failedDraft` 的渲染（#1029 P2：hook 把窗外迟到的失败经这条 prop 送回）。 */
function paintWith(failedDraft: { text: string } | null): void {
  act(() => {
    root.render(createElement(Composer, {
      streaming: false,
      onSubmit,
      onSteer,
      onCancel: () => {},
      sessionId: 'sess-1',
      onEnsureSession: async () => null,
      models: [],
      selectedModel: null,
      failedDraft,
    }));
  });
}

const textarea = (): HTMLTextAreaElement => container.querySelector('#composer-input')!;
const cards = (): NodeListOf<HTMLElement> => container.querySelectorAll('.composer-attach-card');
const sendButton = (): HTMLButtonElement =>
  container.querySelector<HTMLButtonElement>('button[aria-label="发送"]')!;

async function typeText(text: string): Promise<void> {
  await act(async () => {
    const ta = textarea();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype, 'value',
    )?.set;
    setter?.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

async function pasteOneImage(): Promise<void> {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  Object.defineProperty(input, 'files', { value: [imageFile('shot.png')], configurable: true });
  await act(async () => {
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await act(async () => {});
  await act(async () => { resolveUpload?.(receipt('sha256:aaa')); });
  expect(cards()[0].dataset.status).toBe('ready');
}

async function clickSend(): Promise<void> {
  await act(async () => { sendButton().click(); });
}

describe('#1029 AC3 — 发送成功才清空（ack 前不清）', () => {
  it('投递悬挂期间：文本与附件都**还在**（修复前此处已空）', async () => {
    paint();
    onSubmit.mockImplementation(hangingDelivery);
    await typeText('这条不能被弄丢');
    await pasteOneImage();

    await clickSend();

    // 请求已出门（实参正确），但落定信号还没到 ⇒ 不得清空。
    expect(onSubmit).toHaveBeenCalledWith('这条不能被弄丢', false, ['sha256:aaa']);
    expect(textarea().value).toBe('这条不能被弄丢');
    expect(cards()).toHaveLength(1);

    // 落定成"已受理"之后才清。
    await act(async () => { settleSubmit?.(true); });
    await act(async () => { await submitted; });
    expect(textarea().value).toBe('');
    expect(cards()).toHaveLength(0);
  });

  it('未被受理（false）：文本原样留住，附件留在栏里供重发', async () => {
    paint();
    onSubmit.mockImplementation(hangingDelivery);
    await typeText('待裁决会话里的这句话');
    await pasteOneImage();

    await clickSend();
    await act(async () => { settleSubmit?.(false); });
    await act(async () => { await submitted; });

    expect(textarea().value).toBe('待裁决会话里的这句话');
    // 附件也不得被消费：它是失败后一键重发的凭据（AC2）。
    expect(cards()).toHaveLength(1);
  });

  it('回调**抛出/拒绝**也按未受理处理：草稿不丢', async () => {
    paint();
    onSubmit.mockImplementation(() => {
      submitted = Promise.reject(new Error('boom'));
      return submitted;
    });
    await typeText('断代理也要留住');

    await clickSend();
    await act(async () => { await submitted!.catch(() => {}); });

    expect(textarea().value).toBe('断代理也要留住');
  });

  it('steer 通道同样受契约保护（两条投递路径统一覆盖）', async () => {
    paint();
    onSteer.mockImplementation(hangingDelivery);
    await typeText('Ctrl+Enter 这条也要留住');

    await act(async () => {
      textarea().dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true,
      }));
    });

    expect(onSteer).toHaveBeenCalledWith('Ctrl+Enter 这条也要留住', false, []);
    expect(textarea().value).toBe('Ctrl+Enter 这条也要留住');
    await act(async () => { settleSubmit?.(false); });
    await act(async () => { await submitted; });
    expect(textarea().value).toBe('Ctrl+Enter 这条也要留住');
  });

  it('同步回调（无结局通道）保持既有语义：提交即清空', async () => {
    // 既有调用方（含各测试夹具）不返回 promise——没有可等的落定信号，
    // 只能维持 #1029 之前的行为，否则输入框永不自动清空。
    paint();
    onSubmit.mockReturnValue(undefined);
    await typeText('同步通道');
    await clickSend();

    expect(textarea().value).toBe('');
    expect(onSubmit).toHaveBeenCalledWith('同步通道', false, []);
  });
});

describe('#1029 — 清空与回填都不许吞掉更新的输入', () => {
  it('提交后用户又打字：受理时只清掉本次提交那段前缀，新输入保留', async () => {
    paint();
    onSubmit.mockImplementation(hangingDelivery);
    await typeText('第一句');
    await clickSend();

    // 投递在途时用户接着写下一句。
    await typeText('第一句第二句');
    await act(async () => { settleSubmit?.(true); });
    await act(async () => { await submitted; });

    expect(textarea().value).toBe('第二句');
  });

  it('用户在途改写为新草稿：受理时原样保留（不按前缀误删）', async () => {
    paint();
    onSubmit.mockImplementation(hangingDelivery);
    await typeText('旧草稿');
    await clickSend();
    await typeText('完全不同的新草稿');

    await act(async () => { settleSubmit?.(true); });
    await act(async () => { await submitted; });

    expect(textarea().value).toBe('完全不同的新草稿');
  });

  it('失败回填不覆盖用户的新草稿（只在空输入框时回填）', async () => {
    paint();
    onSubmit.mockImplementation(hangingDelivery);
    await typeText('失败的那条');
    await clickSend();
    await typeText('我后来写的');

    await act(async () => { settleSubmit?.(false); });
    await act(async () => { await submitted; });

    expect(textarea().value).toBe('我后来写的');
  });

  it('失败时若用户已清空输入框：把失败的原文还回来（AC1 的"仍在输入框"）', async () => {
    paint();
    onSubmit.mockImplementation(hangingDelivery);
    await typeText('被拒的原文');
    await clickSend();
    await typeText('');

    await act(async () => { settleSubmit?.(false); });
    await act(async () => { await submitted; });

    expect(textarea().value).toBe('被拒的原文');
  });
});

/** #1029 P2（独立审查 Correctness P2-1）：**窗外**落定的失败经 `failedDraft` 回到输入框。
 *
 *  这一档的时机与上面全不同：提交时按"已受理"清空了（窗外无从等待结局），失败是
 *  **几十秒后**才迟到的。所以回填不能走 `submit()` 的返回值，只能靠这条 prop。
 *  数据层的证明在 `useSession.sendConflict.test.tsx` 的「#1029 P2」组；这里锁的是
 *  组件侧的最后一跳：prop 到手 ⇒ 文本真的出现在 textarea 里。 */
describe('#1029 P2 — 窗外迟到失败：failedDraft 把原文还回输入框', () => {
  it('输入框为空时：迟到的失败原文出现在输入框里', async () => {
    paintWith(null);
    await typeText('');
    expect(textarea().value).toBe('');

    await act(async () => { paintWith({ text: '窗外迟到的失败原文' }); });

    expect(textarea().value).toBe('窗外迟到的失败原文');
  });

  it('用户已在打新草稿：迟到的回填**不得**覆盖它（与失败回填同一纪律）', async () => {
    paintWith(null);
    await typeText('我正在写新的');

    await act(async () => { paintWith({ text: '窗外迟到的失败原文' }); });

    expect(textarea().value).toBe('我正在写新的');
  });

  it('同一段文本连续失败两次：第二次仍是新对象 ⇒ 输入框再次拿到原文', async () => {
    // 裸 string 状态会被 Object.is 挡掉第二次；这条钉住"每次失败各算一次注入"。
    paintWith(null);
    await typeText('');

    await act(async () => { paintWith({ text: '同一段' }); });
    expect(textarea().value).toBe('同一段');

    // 用户把它清掉（例如手动删空），第二次失败必须还能还回来。
    await typeText('');
    await act(async () => { paintWith({ text: '同一段' }); });

    expect(textarea().value).toBe('同一段');
  });

  it('failedDraft 为 null（没有失败）时不注入任何东西', async () => {
    paintWith(null);
    await typeText('');

    await act(async () => { paintWith(null); });

    expect(textarea().value).toBe('');
  });
});
