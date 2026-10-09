// @vitest-environment jsdom
/** #825（MM-04 / AC6）：原图浮层的「复制原图」**行为**（独立审查 P3-9：e2e 只断言了
 *  按钮可见，点击后的成功 / 失败两条路径没人跑过）。
 *
 *  分两层：本文件钉**组件接线**（点击 → 调 lib → 显示「已复制」/「复制失败」），
 *  `../lib/clipboardImage.test.ts` 钉**函数本身**（PNG 重编码 + clipboard.write +
 *  失败原样抛出）。所以这里 mock 的是 lib 的边界，不是 navigator。
 */

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { act, createElement } from 'react';
import { createRoot, type Root } from 'react-dom/client';

vi.mock('../lib/clipboardImage', () => ({ copyImageToClipboard: vi.fn() }));

import { copyImageToClipboard } from '../lib/clipboardImage';
import { ImageLightbox } from './ImageLightbox';

let container: HTMLDivElement;
let root: Root;
let onClose: Mock<() => void>;
let copy: Mock<(src: string) => Promise<void>>;

beforeEach(() => {
  (globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  onClose = vi.fn<() => void>();
  copy = vi.mocked(copyImageToClipboard);
  copy.mockReset();
});

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

/** 复制按钮 = 动作区里第一个 `<button>`（下载是 `<a>`、关闭是最后那个按钮）。 */
const copyButton = (): HTMLButtonElement => document.querySelector('button.image-lightbox-btn')!;

function paint(src: string | null): void {
  act(() => {
    root.render(createElement(ImageLightbox, { open: true, onClose, src, name: '图片' }));
  });
}

describe('ImageLightbox「复制原图」（#825 AC6）', () => {
  it('点击 → 对当前 src 调用 copyImageToClipboard → 按钮变「已复制」', async () => {
    copy.mockResolvedValue(undefined);
    paint('blob:lightbox-src');

    expect(copyButton().textContent).toContain('复制原图');
    await act(async () => {
      copyButton().click();
    });

    expect(copyImageToClipboard).toHaveBeenCalledWith('blob:lightbox-src');
    expect(copyButton().textContent).toContain('已复制');
  });

  it('复制失败 → 就地显示原因并引导下载（静默失败等于骗用户"已复制"）', async () => {
    copy.mockRejectedValue(new Error('NotAllowedError: 剪贴板权限被拒'));
    paint('blob:lightbox-src');

    await act(async () => {
      copyButton().click();
    });

    const alert = document.querySelector('.image-lightbox-error');
    expect(alert?.getAttribute('role')).toBe('alert');
    expect(alert?.textContent).toContain('NotAllowedError: 剪贴板权限被拒');
    expect(alert?.textContent).toContain('下载原图');
    // 失败后按钮回到可重试态（不是卡在「复制中…」）。
    expect(copyButton().textContent).toContain('复制原图');
  });

  it('src 不可用 → 复制按钮禁用、点击不进 lib，正文给出"图片不可用"', async () => {
    paint(null);
    expect(copyButton().disabled).toBe(true);
    await act(async () => {
      copyButton().click();
    });
    expect(copyImageToClipboard).not.toHaveBeenCalled();
    expect(document.querySelector('.image-lightbox-empty')?.textContent).toBe('图片不可用');
  });
});
