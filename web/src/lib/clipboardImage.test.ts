// @vitest-environment jsdom
/** #825（MM-04 / AC6）：把原图写进剪贴板的**行为**测试（独立审查 P3-9：此前只有 e2e
 *  的"按钮可见"断言，`copyImageToClipboard` 的成功与失败两条分支零命中）。
 *
 *  jsdom 三样都缺：`ClipboardItem`、canvas 的 2D 上下文、`createImageBitmap`，所以本
 *  文件把这三样按**最小契约**装上（不是给被测代码打桩，而是补宿主 API）；断言的仍是
 *  被测函数自己的行为：重绘到 canvas、转 PNG、把 PNGBlob 交给 `clipboard.write`；
 *  失败时**原样抛出**（不静默吞掉——调用方靠它渲染"复制失败 + 改用下载"）。
 */

import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest';
import { copyImageToClipboard } from './clipboardImage';

/** 最小 `ClipboardItem`（jsdom 无此全局；契约只用到"构造 + 持有 items 字典"）。 */
class FakeClipboardItem {
  readonly items: Record<string, Blob>;
  constructor(items: Record<string, Blob>) {
    this.items = items;
  }
}

const SOURCE_BLOB = new Blob([new Uint8Array([1, 2, 3])], { type: 'image/webp' });
const PNG_BLOB = new Blob([new Uint8Array([9, 9])], { type: 'image/png' });

let write: Mock<(items: FakeClipboardItem[]) => Promise<void>>;
let drawImage: Mock<(image: unknown, dx: number, dy: number) => void>;
let bitmapClose: Mock<() => void>;
let fetchBlob: Mock<() => Promise<Blob>>;

beforeEach(() => {
  write = vi.fn<(items: FakeClipboardItem[]) => Promise<void>>(async () => {});
  drawImage = vi.fn<(image: unknown, dx: number, dy: number) => void>();
  bitmapClose = vi.fn<() => void>();
  fetchBlob = vi.fn(async () => SOURCE_BLOB);

  vi.stubGlobal('ClipboardItem', FakeClipboardItem);
  Object.defineProperty(navigator, 'clipboard', { value: { write }, configurable: true });
  vi.stubGlobal('fetch', vi.fn(async () => ({ blob: fetchBlob })));
  // 位图只被用到宽高与 close()：这两个字段就是被测代码消费的全部契约。
  vi.stubGlobal(
    'createImageBitmap',
    vi.fn(async () => ({ width: 4, height: 2, close: bitmapClose })),
  );
  const context = { drawImage };
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(
    // jsdom 没有 canvas 实现（真 getContext 返回 null 并打 warn）——这里补最小 2D 上下文。
    () => context as unknown as CanvasRenderingContext2D,
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'toBlob').mockImplementation((callback) => {
    callback(PNG_BLOB);
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe('copyImageToClipboard（#825 AC6）', () => {
  it('成功：重绘原图 → 转 PNG → 把一个 image/png 的 ClipboardItem 交给 clipboard.write', async () => {
    await copyImageToClipboard('data:image/webp;base64,AA');

    // 走的是 fetch(src) 的字节，不是把 URL 直接当图片塞进剪贴板。
    expect(fetchBlob).toHaveBeenCalledTimes(1);
    expect(drawImage).toHaveBeenCalledTimes(1);
    expect(bitmapClose).toHaveBeenCalledTimes(1); // 位图用完就关（不泄漏解码缓冲）
    expect(write).toHaveBeenCalledTimes(1);

    const [item] = write.mock.calls[0][0];
    expect(item).toBeInstanceOf(FakeClipboardItem);
    // 统一转 PNG：WebP/JPEG 各家剪贴板支持不一（见模块头）。
    expect(Object.keys(item.items)).toEqual(['image/png']);
    expect(item.items['image/png']).toBe(PNG_BLOB);
  });

  it('浏览器不支持 ClipboardItem → 明确抛错，不静默当成功', async () => {
    vi.stubGlobal('ClipboardItem', undefined);
    await expect(copyImageToClipboard('data:image/png;base64,AA')).rejects.toThrow(
      '当前浏览器不支持写入图片到剪贴板',
    );
    expect(write).not.toHaveBeenCalled();
  });

  it('clipboard.write 被拒（权限/非用户手势）→ 原因原样抛出（调用方据此显示"复制失败"）', async () => {
    write.mockRejectedValueOnce(new Error('NotAllowedError: 剪贴板权限被拒'));
    await expect(copyImageToClipboard('data:image/png;base64,AA')).rejects.toThrow(
      'NotAllowedError: 剪贴板权限被拒',
    );
  });

  it('拿不到 2D 上下文 → 抛"无法创建画布"，不把未重绘的原图塞进剪贴板', async () => {
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);
    await expect(copyImageToClipboard('data:image/png;base64,AA')).rejects.toThrow(
      '无法创建画布（复制需要重编码为 PNG）',
    );
    expect(write).not.toHaveBeenCalled();
  });
});
