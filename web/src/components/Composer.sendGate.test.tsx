// @vitest-environment jsdom
/** #1027（[Web 附图] 附件未就绪时发送被放行）：发送门禁 + 剥离兜底。
 *
 * 判据（逐条对票面验收）：
 *  1. 上传中 → 发送按钮禁用 + 界面常驻「附件上传中，请等待完成」；
 *     Ctrl+Enter 同样被拦截（按钮 disabled 只挡点击，键盘走另一条路）。
 *  2. 上传完成后门禁自动解除，发送带完整 attachments（对照票面 seq 25 正例）。
 *  3. 上传失败 → chip error 态（复用既有三态）；发送**不**被禁用（#825 AC4 保留），
 *     但失败项被显式剔除并给一次性可见提示「N 个附件未随消息发出」——绝不静默丢弃。
 *
 * 为什么 mock `lib/api`：与 `Composer.attachments.test.tsx` 同一条理由——mock 的
 * 只是**传输**（XHR），断言的是本组件自己的行为（门禁/提示/提交实参）。
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

const receipt = (id: string): AttachmentUploadReceipt => ({
  attachment_id: id,
  media_type: 'image/png',
  bytes: 12,
  width: 8,
  height: 8,
});

function setInputFiles(input: HTMLInputElement, files: File[]): void {
  Object.defineProperty(input, 'files', { value: files, configurable: true });
}

function imageFile(name: string, size = 12): File {
  // #937 / M-20：intake 以文件头魔数判型，fixture 必须带真实 PNG 签名。
  const bytes = Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a);
  const file = new File([bytes], name, { type: 'image/png' });
  Object.defineProperty(file, 'size', { value: size });
  return file;
}

let container: HTMLDivElement;
let root: Root;
let onSubmit: Mock<(...args: SubmitArgs) => void>;
let resolveUpload: ((value: AttachmentUploadReceipt) => void) | null;
let rejectUpload: ((error: Error) => void) | null;
/** 每次上传的 resolve/reject（按发起顺序；`resolveUpload`/`rejectUpload` 仍指向最近一次，兼容既有写法）。 */
let pendingUploads: Array<{
  resolve: (value: AttachmentUploadReceipt) => void;
  reject: (error: Error) => void;
}>;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onSubmit = vi.fn<(...args: SubmitArgs) => void>();
  resolveUpload = null;
  rejectUpload = null;
  pendingUploads = [];
  vi.mocked(uploadAttachment).mockImplementation(() => ({
    promise: new Promise<AttachmentUploadReceipt>((resolve, reject) => {
      resolveUpload = resolve;
      rejectUpload = reject;
      pendingUploads.push({ resolve, reject });
    }),
    abort: vi.fn(),
  }));
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
  vi.mocked(uploadAttachment).mockReset();
});

function paint(
  sessionId: string | null = 'sess-1',
  // #1098：pre-intake 测试需要可控的懒建（缺省 = 恒失败；pre-intake 窗口测的是
  // 「在途」，失败形态另有 AC4 用例覆盖）。
  onEnsureSession: () => Promise<string | null> = async () => null,
): void {
  act(() => {
    root.render(
      createElement(Composer, {
        streaming: false,
        onSubmit,
        onCancel: () => {},
        sessionId,
        // #1015：必填 prop；本文件测发送门禁不涉及懒建，给空实现。
        onEnsureSession,
        models: [],
        selectedModel: null,
      }),
    );
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
      window.HTMLTextAreaElement.prototype,
      'value',
    )?.set;
    setter?.call(ta, text);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

/** 粘贴一张图并等它入栏（上传仍在途：promise 故意不 resolve）。
 *  返回入栏后的卡片数（调用方按需断言——同一测试里可能已有别的卡片）。 */
async function pasteOneImage(name = 'shot.png'): Promise<number> {
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
  setInputFiles(input, [imageFile(name)]);
  await act(async () => {
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await act(async () => {});
  return cards().length;
}

function ctrlEnter(): void {
  const event = new KeyboardEvent('keydown', {
    key: 'Enter',
    ctrlKey: true,
    bubbles: true,
    cancelable: true,
  });
  act(() => {
    textarea().dispatchEvent(event);
  });
}

/** 纯 Enter（无修饰键）= queue 通道（§5.1 发送键语义）。 */
function pressEnter(): void {
  const event = new KeyboardEvent('keydown', {
    key: 'Enter',
    bubbles: true,
    cancelable: true,
  });
  act(() => {
    textarea().dispatchEvent(event);
  });
}

describe('Composer 发送门禁：#1027 上传中阻断', () => {
  it('上传中：发送按钮禁用 + 「附件上传中，请等待完成」可见；Ctrl+Enter 被拦截', async () => {
    paint();
    expect(await pasteOneImage()).toBe(1);
    expect(cards()[0].dataset.status).toBe('uploading');
    await typeText('看图说话');

    // 按钮禁用（Gmail 式阻断的第一面）。
    expect(sendButton().disabled).toBe(true);
    expect(sendButton().title).toContain('附件上传中，请等待完成');
    // 可见提示常驻（不只藏在 title 里——键盘用户与读屏用户都要拿到）。
    const hint = container.querySelector('.composer-send-blocked');
    expect(hint).not.toBeNull();
    expect(hint?.getAttribute('role')).toBe('status');
    expect(hint?.textContent).toContain('附件上传中，请等待完成');

    // Ctrl+Enter 走另一条路：同样被拦截，请求不出门。
    ctrlEnter();
    expect(onSubmit).not.toHaveBeenCalled();

    // 点击 disabled 按钮是 no-op（防御性断言：disabled 本来就点不动）。
    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('上传完成后门禁自动解除：发送带完整 attachments，兜底提示不出现', async () => {
    paint();
    expect(await pasteOneImage()).toBe(1);
    await typeText('看图说话');

    await act(async () => {
      resolveUpload?.(receipt('sha256:aaa'));
    });
    expect(cards()[0].dataset.status).toBe('ready');

    // 门禁解除：按钮恢复、提示消失。
    expect(sendButton().disabled).toBe(false);
    expect(container.querySelector('.composer-send-blocked')).toBeNull();

    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).toHaveBeenCalledWith('看图说话', false, ['sha256:aaa']);
    // 全就绪发送 ⇒ 没有被剥离的附件 ⇒ 兜底提示不得出现。
    expect(container.querySelector('.composer-attach-dropped')).toBeNull();
    expect(cards()).toHaveLength(0);
  });

  it('移除在途草稿：门禁随之解除（中止的上传不算未就绪）', async () => {
    paint();
    expect(await pasteOneImage()).toBe(1);
    await typeText('只发文本');
    expect(sendButton().disabled).toBe(true);

    await act(async () => {
      container.querySelector<HTMLButtonElement>('.composer-attach-remove')!.click();
    });
    expect(cards()).toHaveLength(0);
    expect(sendButton().disabled).toBe(false);
    expect(container.querySelector('.composer-send-blocked')).toBeNull();

    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).toHaveBeenCalledWith('只发文本', false, []);
  });
});

describe('Composer 发送兜底：#1027 失败附件显式剔除 + 可见提示', () => {
  it('上传失败：chip error 态；发送不被禁用；失败项被剔除并提示「N 个附件未随消息发出」', async () => {
    paint();
    // 两张：一张就绪（进请求），一张失败（被剔除）——证明是「部分剥离」不是全丢。
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    setInputFiles(input, [imageFile('ok.png')]);
    await act(async () => {
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => {
      resolveUpload?.(receipt('sha256:ok'));
    });
    expect(await pasteOneImage('bad.png')).toBe(2);
    await act(async () => {
      rejectUpload?.(new Error('413 AttachmentMessageTooLarge'));
    });
    expect(cards()).toHaveLength(2);
    expect(cards()[1].dataset.status).toBe('failed');

    await typeText('带一张能用的发');
    // #825 AC4 保留：失败不阻塞文本发送。
    expect(sendButton().disabled).toBe(false);
    await act(async () => {
      sendButton().click();
    });

    // 只有就绪的进了请求；失败的那张被显式剔除。
    expect(onSubmit).toHaveBeenCalledWith('带一张能用的发', false, ['sha256:ok']);
    // 兜底：一次性可见提示——绝不静默丢弃。
    const dropped = container.querySelector('.composer-attach-dropped');
    expect(dropped).not.toBeNull();
    expect(dropped?.getAttribute('role')).toBe('alert');
    expect(dropped?.textContent).toContain('1 个附件未随消息发出');
    // 失败草稿留在栏里供重试（既有行为不变）。
    expect(cards()).toHaveLength(1);
    expect(cards()[0].dataset.status).toBe('failed');

    // 提示可关闭（一次性，不常驻打扰）。
    await act(async () => {
      container
        .querySelector<HTMLButtonElement>('.composer-attach-dropped button[aria-label="关闭附件未发出提示"]')!
        .click();
    });
    expect(container.querySelector('.composer-attach-dropped')).toBeNull();
  });
});

describe('Composer 发送门禁：#1027 复审补齐（Correctness P3 测试缺口）', () => {
  it('纯 Enter（queue 通道）同样被拦截；落定后 Enter 正常发送', async () => {
    paint();
    expect(await pasteOneImage()).toBe(1);
    await typeText('回车发送');

    // queue 通道不走 disabled 按钮的 click，直达 submit——门禁必须同样拦住。
    pressEnter();
    expect(onSubmit).not.toHaveBeenCalled();

    await act(async () => {
      resolveUpload?.(receipt('sha256:aaa'));
    });
    pressEnter();
    expect(onSubmit).toHaveBeenCalledWith('回车发送', false, ['sha256:aaa']);
  });

  it('混合：1 张 ready + 1 张 uploading → 整单被拦截；落定后发送带两张', async () => {
    paint();
    const input = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    setInputFiles(input, [imageFile('a.png')]);
    await act(async () => {
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => {});
    expect(pendingUploads).toHaveLength(1);
    await act(async () => {
      pendingUploads[0].resolve(receipt('sha256:a'));
    });
    expect(cards()[0].dataset.status).toBe('ready');

    setInputFiles(input, [imageFile('b.png')]);
    await act(async () => {
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await act(async () => {});
    expect(pendingUploads).toHaveLength(2);
    expect(cards()[1].dataset.status).toBe('uploading');

    await typeText('两张一起');
    // 整单拦截：不许"先发就绪的那张、把在途的静默丢掉"——那正是 #1027 的形态。
    expect(sendButton().disabled).toBe(true);
    pressEnter();
    expect(onSubmit).not.toHaveBeenCalled();
    ctrlEnter();
    expect(onSubmit).not.toHaveBeenCalled();

    await act(async () => {
      pendingUploads[1].resolve(receipt('sha256:b'));
    });
    expect(sendButton().disabled).toBe(false);
    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).toHaveBeenCalledWith('两张一起', false, ['sha256:a', 'sha256:b']);
  });
});

describe('Composer 兜底提示：#1027 复审 P2（过期文案不清）', () => {
  it('兜底提示是本次发送的结论：干净发送后清除，不误报下一次', async () => {
    paint();
    expect(await pasteOneImage('bad.png')).toBe(1);
    await act(async () => {
      rejectUpload?.(new Error('413 too large'));
    });
    await typeText('第一次');
    await act(async () => {
      sendButton().click();
    });
    expect(container.querySelector('.composer-attach-dropped')).not.toBeNull();

    // 移除失败 chip 后再干净发送 → 旧提示必须清除（否则误报"附件未随消息发出"）。
    await act(async () => {
      container.querySelector<HTMLButtonElement>('.composer-attach-remove')!.click();
    });
    await typeText('第二次');
    await act(async () => {
      sendButton().click();
    });
    expect(onSubmit).toHaveBeenLastCalledWith('第二次', false, []);
    expect(container.querySelector('.composer-attach-dropped')).toBeNull();
  });

  it('换会话即清兜底提示（不跟去新会话）', async () => {
    paint('sess-1');
    expect(await pasteOneImage('bad.png')).toBe(1);
    await act(async () => {
      rejectUpload?.(new Error('413 too large'));
    });
    await typeText('在旧会话发');
    await act(async () => {
      sendButton().click();
    });
    expect(container.querySelector('.composer-attach-dropped')).not.toBeNull();

    // 换会话：草稿被 hook 清空，提示也不得残留。
    paint('sess-2');
    expect(cards()).toHaveLength(0);
    expect(container.querySelector('.composer-attach-dropped')).toBeNull();
  });
});

describe('Composer 发送门禁：#1098 pre-intake 窗口阻断（粘贴后立刻 Enter 不拆散）', () => {
  it('ensureSession 在途（草稿未入列）：按钮禁用 + 可见原因；Enter/Ctrl+Enter 被拦截', async () => {
    let resolveSession!: (id: string | null) => void;
    const ensureSession = vi.fn(
      () =>
        new Promise<string | null>((resolve) => {
          resolveSession = resolve;
        }),
    );
    paint(null, ensureSession);
    // 新建态投喂：懒建被闸住 → 草稿未入列（pre-intake 窗口），栏里 0 张。
    expect(await pasteOneImage()).toBe(0);
    await typeText('看这张图');

    // 门禁：按钮禁用 + 常驻可见原因（复用 #1027 的 SEND_BLOCKED_REASON 体系）。
    expect(sendButton().disabled).toBe(true);
    expect(container.querySelector('.composer-send-blocked')?.textContent).toContain(
      '附件上传中，请等待完成',
    );
    pressEnter();
    expect(onSubmit).not.toHaveBeenCalled();
    ctrlEnter();
    expect(onSubmit).not.toHaveBeenCalled();

    // 懒建落定 + 上传就绪后门禁解除：Enter 图文同发，不拆散。
    await act(async () => {
      resolveSession('sess-new');
    });
    await act(async () => {});
    expect(cards()).toHaveLength(1);
    await act(async () => {
      resolveUpload?.(receipt('sha256:aaa'));
    });
    expect(sendButton().disabled).toBe(false);
    expect(container.querySelector('.composer-send-blocked')).toBeNull();
    pressEnter();
    expect(onSubmit).toHaveBeenCalledWith('看这张图', false, ['sha256:aaa']);
  });

  it('懒建失败：pre-intake 清零，纯文本仍可发（AC4 不变，#1098 不扩大阻断）', async () => {
    paint(null, async () => null);
    expect(await pasteOneImage()).toBe(0);
    await typeText('纯文本');
    // intake 失败走 hook 的 finally 清 pending：门禁不残留，文本照发。
    expect(sendButton().disabled).toBe(false);
    expect(container.querySelector('.composer-send-blocked')).toBeNull();
    pressEnter();
    expect(onSubmit).toHaveBeenCalledWith('纯文本', false, []);
    // 失败可见（AC3 同一通道），不留"永远上传中"卡片。
    expect(container.querySelector('.composer-attach-error[role="alert"]')?.textContent).toContain(
      '会话创建失败',
    );
  });
});
