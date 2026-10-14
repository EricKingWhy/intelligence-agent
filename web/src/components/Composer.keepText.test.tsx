// @vitest-environment jsdom
/** #1029：发送**失败**时 composer 必须留住用户文本（数据丢失型缺陷的回归锁）。
 *
 *  规格依据（不是 UX 偏好）：`SPEC_ROOT/11_STREAMING_API_WEB_UI.md` §6.1 第 164 行
 *  ——「409 冲突后 MUST 刷新权威状态并**保留用户未发送的输入**」。
 *
 *  缺陷形态：`submit()` 在调用 `onSubmit` 之后**无条件**同步 `setValue('')`，
 *  而 `onSubmit` 返回 void ⇒ 任何发送失败（待裁决 409 / 网络断 / 429…）用户文本
 *  无声消失，且没有任何补偿通道。
 *
 *  契约（本文件锁死）：`onSubmit`/`onSteer` 返回 `false` = 这次发送**没被受理**
 *  ⇒ 文本与附件引用原样留在 composer；返回 `undefined`（旧签名，零回归）或 `true`
 *  ⇒ 照旧清空。
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

/** 提交口签名：`false` = 未受理（保留输入）；`undefined`/`true` = 已受理（清空）。 */
type SubmitArgs = [task: string, rememberAsProceduralRule: boolean, attachmentIds?: string[]];
type SubmitResult = void | boolean;

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

let container: HTMLDivElement;
let root: Root;
let onSubmit: Mock<(...args: SubmitArgs) => SubmitResult>;
let onSteer: Mock<(...args: SubmitArgs) => SubmitResult>;

const textarea = (): HTMLTextAreaElement => container.querySelector('#composer-input')!;
const cards = (): NodeListOf<HTMLElement> => container.querySelectorAll('.composer-attach-card');

async function mount(): Promise<void> {
  await act(async () => {
    root.render(
      createElement(Composer, {
        streaming: false,
        onSubmit,
        onSteer,
        onCancel: () => {},
        onEnsureSession: async () => 's-1',
        sessionId: 's-1',
        models: [],
        selectedModel: null,
      }),
    );
  });
}

async function typeText(text: string): Promise<void> {
  await act(async () => {
    const ta = textarea();
    const setter = Object.getOwnPropertyDescriptor(
      window.HTMLTextAreaElement.prototype,
      'value',
    )?.set;
    setter?.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

function pressEnter(): void {
  act(() => {
    textarea().dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    );
  });
}

function ctrlEnter(): void {
  act(() => {
    textarea().dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Enter', ctrlKey: true, bubbles: true, cancelable: true }),
    );
  });
}

beforeEach(async () => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onSubmit = vi.fn<(...args: SubmitArgs) => SubmitResult>();
  onSteer = vi.fn<(...args: SubmitArgs) => SubmitResult>();
  vi.mocked(uploadAttachment).mockImplementation(() => ({
    promise: Promise.resolve(receipt('sha256:aa')),
    abort: () => {},
  }) as unknown as ReturnType<typeof uploadAttachment>);
  await mount();
});

afterEach(async () => {
  await act(async () => { root.unmount(); });
  container.remove();
  vi.clearAllMocks();
});

describe('#1029 composer 发送失败保留文本', () => {
  it('未受理（onSubmit → false）：文本留在输入框', async () => {
    onSubmit.mockReturnValue(false);
    await typeText('待裁决会话的文本不能丢');
    pressEnter();

    expect(onSubmit).toHaveBeenCalledWith('待裁决会话的文本不能丢', false, []);
    expect(textarea().value).toBe('待裁决会话的文本不能丢');
  });

  it('未受理（异步 resolve false）：文本同样留在输入框（等待回执期间不清）', async () => {
    let settle: ((ok: boolean) => void) | null = null;
    onSubmit.mockReturnValue(
      new Promise<boolean>((resolve) => {
        settle = resolve;
      }) as unknown as SubmitResult,
    );
    await typeText('网络断了也不能丢');
    pressEnter();

    // ack 之前：不清空
    expect(textarea().value).toBe('网络断了也不能丢');
    await act(async () => {
      settle!(false);
    });
    // 失败落定：仍然保留
    expect(textarea().value).toBe('网络断了也不能丢');
  });

  it('已受理（异步 resolve true）：ack 之后才清空', async () => {
    let settle: ((ok: boolean) => void) | null = null;
    onSubmit.mockReturnValue(
      new Promise<boolean>((resolve) => {
        settle = resolve;
      }) as unknown as SubmitResult,
    );
    await typeText('正常会话');
    pressEnter();

    expect(textarea().value).toBe('正常会话');
    await act(async () => {
      settle!(true);
    });
    expect(textarea().value).toBe('');
  });

  /* P3（独立审查发现）：清空挪到 ack 之后 ⇒ 在途期间输入框是**活的**（未禁用），
     用户接着打的字会在回执到达时被无条件 `setValue('')` 一起抹掉——与
     `sendingRef` 挡住的「重复提交」是同一个窗口的两种表现。
     取代价小的那个修法：只清**本次提交的那段前缀**，新打的字留下。 */
  it('ack 在途期间继续输入：只清本次提交的那段，新打的字不被抹掉', async () => {
    let settle: ((ok: boolean) => void) | null = null;
    onSubmit.mockReturnValue(
      new Promise<boolean>((resolve) => {
        settle = resolve;
      }) as unknown as SubmitResult,
    );
    await typeText('第一段');
    pressEnter();
    // 回执未落定，用户接着往后打（输入框此时没有被禁用）
    await typeText('第一段 接着打的新内容');

    await act(async () => {
      settle!(true);
    });

    // 本次提交的那段清掉，在途期间新打的字留下（修复前：整框被清成 ''）
    expect(textarea().value).toBe(' 接着打的新内容');
  });

  it('旧签名（onSubmit 返回 undefined）：照旧清空——既有调用零回归', async () => {
    onSubmit.mockReturnValue(undefined);
    await typeText('既有调用');
    pressEnter();

    /* 清空挪到了回执之后（`Promise.resolve(undefined).then`），所以断言要等一个
       微任务——**时序变了、契约没变**：`undefined` 仍表示已受理、照旧清空。
       （初版这条直接同步断言 `''`，它其实锁的是「提交瞬间同步清空」这个**病态**
       时序本身——正是本票要拿掉的东西。红-绿记录见 out 文件。） */
    await act(async () => {});
    expect(textarea().value).toBe('');
  });

  it('steer 通道同样覆盖：未受理时保留文本', async () => {
    onSteer.mockReturnValue(false);
    await typeText('Ctrl+Enter 那条路');
    ctrlEnter();

    expect(onSteer).toHaveBeenCalledWith('Ctrl+Enter 那条路', false, []);
    expect(textarea().value).toBe('Ctrl+Enter 那条路');
  });

  it('未受理时附件引用不被消费（clearSent 不执行）', async () => {
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(input, 'files', { value: [imageFile('shot.png')], configurable: true });
    await act(async () => {
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => {});
    expect(cards().length).toBe(1);

    onSubmit.mockReturnValue(false);
    await typeText('带图的失败发送');
    pressEnter();

    expect(onSubmit).toHaveBeenCalledWith('带图的失败发送', false, ['sha256:aa']);
    // 文本与草稿都还在（草稿没被 clearSent 摘掉）
    expect(textarea().value).toBe('带图的失败发送');
    expect(cards().length).toBe(1);
  });

  it('在途未落定时重复提交被挡住（不会因为保留文本而重复发送）', async () => {
    onSubmit.mockReturnValue(
      new Promise<boolean>(() => {
        /* 永不落定 */
      }) as unknown as SubmitResult,
    );
    await typeText('只该发一次');
    pressEnter();
    pressEnter();
    pressEnter();

    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});
