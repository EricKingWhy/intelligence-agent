// @vitest-environment jsdom
/** #825（MM-04）：Composer 附图 intake 的三条通道 + 状态机 + AC4/AC7 的判定。
 *
 *  为什么必须真实客户端渲染（而不是 SSR 车道）：判据是 DOM 事件（paste / drop /
 *  change）、Portal（拖放遮罩）、以及"上传进行中"这种异步状态——`renderToString`
 *  三样都观察不到（先例：`Composer.presetFocus.test.tsx` / `ApprovalModal.test.tsx`）。
 *
 *  为什么 mock `lib/api`：上传是 XHR（jsdom 没有真网络）。mock 的只是**传输**，
 *  断言的是本组件自己的行为：入栏、进度/失败态、重试、以及"哪些 id 进了提交"
 *  ——即「上传失败不得阻塞纯文本发送」（AC4）与「只带已就绪引用」这两条。
 */

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return { ...actual, uploadAttachment: vi.fn() };
});

import {
  uploadAttachment,
  type AttachmentUploadReceipt,
  type ModelCatalogEntry,
} from '../lib/api';
import { Composer } from './Composer';

/** 提交口的最小签名（`Composer` 的 `onSubmit`），显式命名以便断言实参。 */
type SubmitArgs = [task: string, rememberAsProceduralRule: boolean, attachmentIds?: string[]];

const receipt = (id: string): AttachmentUploadReceipt => ({
  attachment_id: id,
  media_type: 'image/png',
  bytes: 12,
  width: 8,
  height: 8,
});

/** 让 jsdom 里的 `<input type="file">` 拿到文件（`files` 是只读 getter）。 */
function setInputFiles(input: HTMLInputElement, files: File[]): void {
  Object.defineProperty(input, 'files', { value: files, configurable: true });
}

function imageFile(name: string, type = 'image/png', size = 12): File {
  // #937 / M-20 起 intake 以文件头魔数判型（`detectImageMediaType`）：空壳文件会
  // 被 fail-closed 判为"非图片"。fixture 必须带真实签名——image/png 给 PNG 头，
  // 其余类型给探测不认识的字节（走拒收路径，如 notes.pdf）。
  const bytes =
    type === 'image/png'
      ? Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)
      : Uint8Array.of(0x25, 0x50, 0x44, 0x46, 0x2d);
  const file = new File([bytes], name, { type });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

/** 只提供 dropEvents 需要的字段（jsdom 无 DataTransfer 构造器）。 */
function fakeTransfer(files: File[]): DataTransfer {
  return {
    types: ['Files'],
    files,
    items: files.map(() => ({ kind: 'file', webkitGetAsEntry: () => null })),
    dropEffect: 'none',
  } as unknown as DataTransfer;
}

function dispatchDrag(type: 'dragenter' | 'dragover' | 'dragleave' | 'drop', files: File[]): void {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'dataTransfer', { value: fakeTransfer(files) });
  Object.defineProperty(event, 'clientX', { value: 10 });
  Object.defineProperty(event, 'clientY', { value: 10 });
  document.dispatchEvent(event);
}

/** 造一次粘贴事件。`text` = 同一个剪贴板里 `text/plain` 的内容（网页 / Word 会给
 *  「图 + 文本」的混合剪贴板；纯文本粘贴的用例传空串）。 */
function pasteFiles(textarea: HTMLTextAreaElement, files: File[], text = ''): Event {
  const event = new Event('paste', { bubbles: true, cancelable: true });
  Object.defineProperty(event, 'clipboardData', {
    value: {
      items: files.map((file) => ({ kind: 'file', type: file.type, getAsFile: () => file })),
      getData: (format: string) => (format === 'text/plain' ? text : ''),
    },
  });
  act(() => {
    textarea.dispatchEvent(event);
  });
  // 返回事件：`defaultPrevented` 就是"有没有把粘贴吞掉"的判据（入口被门禁挡住时
  // 必须放行原生粘贴，否则网页/Word 那种"图 + 文本"的混合剪贴板连文本也丢）。
  return event;
}

let container: HTMLDivElement;
let root: Root;
let onSubmit: Mock<(...args: SubmitArgs) => void>;
let resolveUpload: ((value: AttachmentUploadReceipt) => void) | null;
let rejectUpload: ((error: Error) => void) | null;
let abortSpy: Mock<() => void>;
let progressCallback: ((loaded: number, total: number) => void) | null;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onSubmit = vi.fn<(...args: SubmitArgs) => void>();
  resolveUpload = null;
  rejectUpload = null;
  progressCallback = null;
  abortSpy = vi.fn<() => void>();
  vi.mocked(uploadAttachment).mockImplementation((_sessionId, _file, onProgress) => {
    progressCallback = onProgress ?? null;
    return {
      promise: new Promise<AttachmentUploadReceipt>((resolve, reject) => {
        resolveUpload = resolve;
        rejectUpload = reject;
      }),
      abort: abortSpy,
    };
  });
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.mocked(uploadAttachment).mockReset();
});

function paint(props: {
  sessionId?: string | null;
  models?: ModelCatalogEntry[];
  selectedModel?: string | null;
}): void {
  act(() => {
    root.render(
      createElement(Composer, {
        streaming: false,
        onSubmit,
        onCancel: () => {},
        sessionId: props.sessionId === undefined ? 'sess-1' : props.sessionId,
        models: props.models ?? [],
        selectedModel: props.selectedModel ?? null,
      }),
    );
  });
}

const textarea = (): HTMLTextAreaElement => container.querySelector('#composer-input')!;
const cards = (): NodeListOf<HTMLElement> => container.querySelectorAll('.composer-attach-card');
const sendButton = (): HTMLButtonElement =>
  container.querySelector<HTMLButtonElement>('button[aria-label="发送"]')!;
const attachButton = (): HTMLButtonElement =>
  container.querySelector<HTMLButtonElement>('button[aria-label="添加图片"]')!;

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

describe('Composer 附图：#825 AC1 三条 intake 通道', () => {
  it('粘贴图片文件 → 入栏（缩略图卡 + 预算），文本粘贴不 preventDefault', async () => {
    paint({});
    pasteFiles(textarea(), [imageFile('shot.png')]);
    // #937 / M-20：addFiles 内部是 async IIFE（字节探测），入栏晚一个微任务。
    await act(async () => {});

    expect(cards()).toHaveLength(1);
    expect(container.querySelector('.composer-attach-budget')?.textContent).toContain('已附 1/20 张');
    expect(container.querySelector('.composer-attach-name')?.textContent).toBe('shot.png');

    // 纯文本粘贴：不吞事件（真实浏览器里这决定文本能否进输入框）。
    const textPaste = new Event('paste', { bubbles: true, cancelable: true });
    Object.defineProperty(textPaste, 'clipboardData', { value: { items: [] } });
    act(() => {
      textarea().dispatchEvent(textPaste);
    });
    expect(textPaste.defaultPrevented).toBe(false);
  });

  it('混合剪贴板（图 + 文本）：接管事件后 text/plain 插到光标处，文本不丢', async () => {
    paint({});
    await typeText('看图吧');
    const ta = textarea();
    // 光标落在「看图」与「吧」之间：插入位置必须真按光标算，不能只往末尾追加。
    ta.setSelectionRange(2, 2);
    const event = pasteFiles(ta, [imageFile('shot.png')], '（来自网页）');
    await act(async () => {});

    // 有文件 ⇒ 事件被接管（图要入栏），文本由**我们**回填（不回填就等于吞掉）。
    expect(event.defaultPrevented).toBe(true);
    expect(cards()).toHaveLength(1);
    expect(textarea().value).toBe('看图（来自网页）吧');
    // 受控 textarea 的 value 由 React 写回，插入符位置由渲染后的 effect 补回；
    // 不补的话光标留在末尾，用户接着敲的字会跑到粘贴内容之后。
    expect(textarea().selectionStart).toBe(2 + '（来自网页）'.length);
  });

  it('文件选择器 → 入栏；同一张图可再次选择（onChange 后 value 被清空）', async () => {
    paint({});
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    const file = imageFile('a.png');
    setInputFiles(input, [file]);
    await act(async () => {
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    expect(cards()).toHaveLength(1);
    expect(input.value).toBe('');
  });

  it('整页拖放：dragenter 出遮罩，drop 入栏；拖放遮罩对拖放透明（pointer-events）', async () => {
    paint({});
    await act(async () => {
      dispatchDrag('dragenter', [imageFile('d.png')]);
    });
    expect(document.querySelector('.drop-mask')).not.toBeNull();
    expect(document.querySelector('.drop-title')?.textContent).toContain('松开鼠标');

    await act(async () => {
      dispatchDrag('drop', [imageFile('d.png')]);
    });
    await act(async () => {});
    expect(document.querySelector('.drop-mask')).toBeNull();
    expect(cards()).toHaveLength(1);
  });
});

describe('Composer 附图：#825 AC2/AC3 预检与提示', () => {
  it('超限整批被拒：卡片不入栏，原因常驻并带文件名与上限', async () => {
    paint({});
    const huge = imageFile('huge.png', 'image/png', 21 * 1024 * 1024);
    pasteFiles(textarea(), [huge, imageFile('ok.png')]);
    await act(async () => {});

    expect(cards()).toHaveLength(0);
    const alert = container.querySelector('.composer-attach-error[role="alert"]');
    expect(alert?.textContent).toContain('huge.png');
    expect(alert?.textContent).toContain('超过单张上限 20 MiB');
    // 被拒的整批一个字节都没上传——预检在 intake 之前就把关（AC3 的"不等发送失败"）。
    expect(uploadAttachment).not.toHaveBeenCalled();

    // 常驻：错误提示只有用户关掉才消失（不自动退场）。
    await act(async () => {
      container
        .querySelector<HTMLButtonElement>('button[aria-label="关闭错误提示"]')!
        .click();
    });
    expect(container.querySelector('.composer-attach-error')).toBeNull();
  });

  it('非图片文件被点名拒绝（AC3 要能行动，不是一句"上传失败"）', async () => {
    paint({});
    pasteFiles(textarea(), [imageFile('notes.pdf', 'application/pdf')]);
    await act(async () => {});
    expect(container.querySelector('.composer-attach-error')?.textContent).toContain('notes.pdf');
  });
});

describe('Composer 附图：#825 AC4 上传状态、失败与重试', () => {
  it('上传中就绪前后：先 uploading，resolve 后 ready；发送只带已就绪引用', async () => {
    paint({});
    pasteFiles(textarea(), [imageFile('a.png')]);
    await act(async () => {});
    expect(cards()[0].dataset.status).toBe('uploading');

    // 进度来自 xhr.upload.onprogress 转述（回调由 api 层交给 hook）。
    await act(async () => {
      progressCallback?.(6, 12);
    });
    expect(container.querySelector('.composer-attach-size')?.textContent).toBe('上传中 50%');

    await act(async () => {
      resolveUpload?.(receipt('sha256:aaa'));
    });
    expect(cards()[0].dataset.status).toBe('ready');

    await typeText('看这张图');
    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).toHaveBeenCalledWith('看这张图', false, ['sha256:aaa']);
    // 已发送的草稿离开栏（下一轮发送不会重复带同一张图）。
    expect(cards()).toHaveLength(0);
  });

  it('上传失败：卡片给出原因与重试；**纯文本发送不被阻塞**，且失败项不进请求', async () => {
    paint({});
    pasteFiles(textarea(), [imageFile('bad.png')]);
    await act(async () => {});
    await act(async () => {
      rejectUpload?.(new Error('413 AttachmentMessageTooLarge'));
    });

    expect(cards()[0].dataset.status).toBe('failed');
    expect(container.querySelector('.composer-attach-failed')?.textContent).toContain(
      '413 AttachmentMessageTooLarge',
    );

    await typeText('没有图也照发');
    expect(sendButton().disabled).toBe(false);
    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).toHaveBeenCalledWith('没有图也照发', false, []);
    // 失败的草稿留在栏里（它没进请求），供重试。
    expect(cards()).toHaveLength(1);

    // 重试成功 → 变 ready → 下一次发送带上它。
    await act(async () => {
      container.querySelector<HTMLButtonElement>('.composer-attach-failed button')!.click();
    });
    expect(cards()[0].dataset.status).toBe('uploading');
    await act(async () => {
      resolveUpload?.(receipt('sha256:bbb'));
    });
    expect(cards()[0].dataset.status).toBe('ready');
    await typeText('再试一次');
    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).toHaveBeenLastCalledWith('再试一次', false, ['sha256:bbb']);
  });

  it('移除草稿：中止在途上传并离开栏', async () => {
    paint({});
    pasteFiles(textarea(), [imageFile('a.png')]);
    await act(async () => {});
    await act(async () => {
      container.querySelector<HTMLButtonElement>('.composer-attach-remove')!.click();
    });
    expect(abortSpy).toHaveBeenCalledTimes(1);
    expect(cards()).toHaveLength(0);
  });
});

describe('Composer 附图：#825 AC7 入口门禁', () => {
  const visionOff: ModelCatalogEntry[] = [
    { name: 'text-only', default: true, supportsVision: false },
  ] as ModelCatalogEntry[];

  it('supports_vision=false：入口禁用 + 可见原因，粘贴/拖放都不入栏', async () => {
    paint({ models: visionOff });
    expect(attachButton().disabled).toBe(true);
    const hint = container.querySelector('.composer-attach-hint');
    expect(hint?.textContent).toContain('supports_vision=false');

    // #937 / M-24：title 与可见提示同源（NON_VISION_MODEL_REASON 常量），逐字节钉住
    // 两串——抽常量后任何一侧漂移都立刻红，而不是靠人眼对。
    expect(hint?.textContent).toBe(
      '当前模型不支持视觉（supports_vision=false）：已禁用附图；历史附图会被省略为文本占位',
    );

    // 粘贴：不入栏，且**不接管**事件（`preventDefault` 一开，混合剪贴板里的文本也丢）。
    const pasteEvent = pasteFiles(textarea(), [imageFile('a.png')]);
    expect(pasteEvent.defaultPrevented).toBe(false);
    await act(async () => {
      dispatchDrag('dragenter', [imageFile('a.png')]);
    });
    // 遮罩给的是**禁止**插图 + 原因，而不是"松开即可"。（M-24：与 title 逐字节同源）
    expect(document.querySelector('.drop-title')?.textContent).toBe(
      '当前模型不支持视觉（supports_vision=false），已禁用附图',
    );
    await act(async () => {
      dispatchDrag('drop', [imageFile('a.png')]);
    });
    expect(cards()).toHaveLength(0);
    expect(uploadAttachment).not.toHaveBeenCalled();
  });

  it('supportsVision 缺失（后端沉默）→ 不误判为不支持，入口可用', () => {
    paint({ models: [{ name: 'unknown', default: true }] as ModelCatalogEntry[] });
    expect(attachButton().disabled).toBe(false);
    expect(container.querySelector('.composer-attach-hint')).toBeNull();
  });

  it('无会话（新建态）：入口禁用并说明原因（上传端点挂在会话上）', () => {
    paint({ sessionId: null });
    expect(attachButton().disabled).toBe(true);
    expect(attachButton().title).toContain('需要先有会话');
    // 同样不得吞掉粘贴：新建态下用户往往是"粘一段文字顺便带张截图"。
    expect(pasteFiles(textarea(), [imageFile('a.png')]).defaultPrevented).toBe(false);
    expect(cards()).toHaveLength(0);
  });
});
