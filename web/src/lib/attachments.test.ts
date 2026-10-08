/** #825（MM-04）AC10：粘贴取文件 + 整批预检 + 预算文案的**纯函数**契约。
 *
 *  为什么值得单独钉：这三条是"用户看得见的拒绝理由"的唯一来源（AC3），
 *  且 AC10 要求粘贴逻辑独立成纯函数供桌面复用——纯函数的好处就该被固定行为锁住，
 *  而不是靠 React 测试间接观察。
 */

import { describe, expect, it } from 'vitest';
import {
  budgetText,
  filesFromClipboard,
  formatBytes,
  IMAGE_LIMITS,
  partitionIntake,
  type ImageIntakeLimits,
} from './attachments';

/** jsdom 有 File；node 环境（本文件默认）从 vitest 的全局拿到。 */
const file = (name: string, type: string, size: number): File => {
  const blob = new Blob([new Uint8Array(0)], { type });
  const f = new File([blob], name, { type });
  Object.defineProperty(f, 'size', { value: size });
  return f;
};

/** 只关心"条目形状"的 DataTransfer 替身（jsdom 的 DataTransfer 构造受限）。 */
const transfer = (items: Array<{ kind: string; file: File | null }>): DataTransfer =>
  ({
    items: items.map((it) => ({
      kind: it.kind,
      type: it.file?.type ?? '',
      getAsFile: () => it.file,
    })),
  }) as unknown as DataTransfer;

describe('filesFromClipboard — AC10', () => {
  it('只取 kind === "file" 的项（文本粘贴返回空数组 ⇒ 调用方不得 preventDefault）', () => {
    const img = file('a.png', 'image/png', 10);
    const data = transfer([
      { kind: 'string', file: null },
      { kind: 'file', file: img },
    ]);
    expect(filesFromClipboard(data)).toEqual([img]);
  });

  it('没有文件项 → 空数组（纯文本粘贴走原生路径）', () => {
    expect(filesFromClipboard(transfer([{ kind: 'string', file: null }]))).toEqual([]);
  });

  it('clipboardData 为 null（非剪贴板事件 / 权限被拒）→ 空数组，不抛', () => {
    expect(filesFromClipboard(null)).toEqual([]);
  });

  it('getAsFile 返回 null 的项被跳过（不是 push 一个 null 下去）', () => {
    expect(filesFromClipboard(transfer([{ kind: 'file', file: null }]))).toEqual([]);
  });
});

describe('partitionIntake — AC2/AC3 的整批判定', () => {
  const limits: ImageIntakeLimits = {
    maxImageBytes: 100,
    maxImagesPerMessage: 3,
    maxMessageImageBytes: 250,
    mediaTypes: ['image/png'],
  };

  it('全部合法 → 整批接受', () => {
    const out = partitionIntake([file('a.png', 'image/png', 10), file('b.png', 'image/png', 20)], [], limits);
    expect(out.error).toBeNull();
    expect(out.accepted).toHaveLength(2);
  });

  it('非图片类型 → 整批拒绝并列出文件名（不做部分接受）', () => {
    const out = partitionIntake([file('ok.png', 'image/png', 10), file('doc.pdf', 'application/pdf', 10)], [], limits);
    expect(out.accepted).toEqual([]);
    expect(out.error).toContain('doc.pdf');
    expect(out.error).toContain('只支持 PNG');
  });

  it('数量超限：把"已附几张 + 本次几张"都写出来（AC3 要能行动）', () => {
    const out = partitionIntake(
      [file('c.png', 'image/png', 1), file('d.png', 'image/png', 1)],
      [{ bytes: 1 }, { bytes: 1 }],
      limits,
    );
    expect(out.accepted).toEqual([]);
    expect(out.error).toContain('最多 3 张/条');
    expect(out.error).toContain('已附 2 张 + 本次 2 张');
  });

  it('单张超限：点名是哪个文件、附带实际大小', () => {
    const out = partitionIntake([file('huge.png', 'image/png', 101)], [], limits);
    expect(out.accepted).toEqual([]);
    expect(out.error).toContain('huge.png');
    expect(out.error).toContain('101 B');
    expect(out.error).toContain('100 B');
  });

  it('合计超限：已附字节 + 本次字节都要在文案里（否则用户不知道从哪减）', () => {
    const out = partitionIntake([file('a.png', 'image/png', 60), file('b.png', 'image/png', 60)], [{ bytes: 200 }], limits);
    expect(out.accepted).toEqual([]);
    expect(out.error).toContain('单条总上限 250 B');
    expect(out.error).toContain('已附 200 B + 本次 120 B');
  });

  it('空输入 → 无接受无错误（调用方因此不会渲染空栏）', () => {
    expect(partitionIntake([], [], limits)).toEqual({ accepted: [], error: null });
  });

  it('默认上限与后端 config.py 的默认值同值（漂移会表现为"预检放行、服务端 413/422"）', () => {
    expect(IMAGE_LIMITS.maxImageBytes).toBe(20 * 1024 * 1024);
    expect(IMAGE_LIMITS.maxImagesPerMessage).toBe(20);
    expect(IMAGE_LIMITS.maxMessageImageBytes).toBe(200 * 1024 * 1024);
    expect([...IMAGE_LIMITS.mediaTypes]).toEqual([
      'image/png',
      'image/jpeg',
      'image/webp',
      'image/gif',
    ]);
  });
});

describe('budgetText / formatBytes — AC2 的预算提示', () => {
  it('张数与剩余字节两条都给出', () => {
    const text = budgetText([{ bytes: 1024 * 1024 }, { bytes: 0 }]);
    expect(text).toContain('已附 2/20 张');
    expect(text).toContain('剩余 18 张');
    expect(text).toContain('199');
  });

  it('超出后剩余不为负（显示 0 而不是负数）', () => {
    const text = budgetText([{ bytes: 0 }, { bytes: 0 }, { bytes: 0 }], {
      maxImageBytes: 10,
      maxImagesPerMessage: 2,
      maxMessageImageBytes: 5,
      mediaTypes: ['image/png'],
    });
    expect(text).toContain('已附 3/2 张');
    expect(text).toContain('剩余 0 张');
  });

  it('formatBytes 用 1024 进制（与后端 MiB 口径一致）', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KiB');
    expect(formatBytes(20 * 1024 * 1024)).toBe('20 MiB');
    expect(formatBytes(1.5 * 1024 * 1024)).toBe('1.5 MiB');
  });
});
