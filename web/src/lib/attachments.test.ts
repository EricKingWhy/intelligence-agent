/** #825（MM-04）：附图**纯函数层**的契约——`filesFromClipboard`（AC10）、
 *  `partitionIntake`（AC2/AC3 的整批判定）、`budgetText`（AC2 预算）。
 *  #937 / M-22 起 `formatBytes` 收口到 `format.ts`（唯一实现），其直测随迁到
 *  `format.test.ts`；本文件只钉预算文案引用它产出的 MiB 行为。
 *
 *  为什么值得单独钉：这些函数是"用户看得见的拒绝理由"的唯一来源（AC3 要求点名
 *  文件与具体上限），且 AC10 明确要求粘贴取文件逻辑独立成纯函数供桌面复用——
 *  纯函数的契约就该被直接锁住，而不是靠 React 测试间接观察。
 *
 *  这里**不**测通道接线（粘贴/拖放/选择器如何触发）与上传状态机：那两条在
 *  `components/Composer.attachments.test.tsx`（需要真事件与 jsdom 环境），
 *  `data:` 缩略图与受控读回在 `e2e/image-attachments.spec.ts`（需要真浏览器）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  budgetText,
  detectImageMediaType,
  filesFromClipboard,
  getImageLimits,
  IMAGE_LIMITS,
  loadImageLimits,
  partitionIntake,
  setImageLimits,
  sumBytes,
  type ImageIntakeLimits,
} from './attachments';

/** jsdom 有 File；node 环境（本文件默认）从 vitest 的全局拿到。 */
const file = (name: string, type: string, size: number): File => {
  const blob = new Blob([new Uint8Array(0)], { type });
  const f = new File([blob], name, { type });
  Object.defineProperty(f, 'size', { value: size });
  return f;
};

/** 探针测试专用：真实字节构造 File（空 blob + 伪造 size 的 `file` 骗不过字节探测，
 *  也让 `file.slice(0,16).arrayBuffer()` 拿到的是真字节）。 */
const byteFile = (name: string, bytes: Uint8Array<ArrayBuffer>, type = ''): File =>
  new File([bytes], name, { type });

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

/**
 * #937 / M-20：字节探测（判据经由后端 `probe.py::_detect` 对齐，其来源登记见该
 * 文件头）。客户端声明的 `file.type` 可伪造（`.txt` 改名 `.png` 即可骗过），
 * 预检的「是不是图片」必须由字节回答。
 */
describe('detectImageMediaType — #937 / M-20 字节探测', () => {
  it('PNG 魔数（89 50 4E 47 0D 0A 1A 0A）→ image/png', async () => {
    const bytes = Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52);
    await expect(detectImageMediaType(byteFile('a.png', bytes, 'image/png'))).resolves.toBe('image/png');
  });

  it('GIF87a → image/gif', async () => {
    const bytes = new TextEncoder().encode('GIF87a');
    await expect(detectImageMediaType(byteFile('a.gif', bytes, 'image/gif'))).resolves.toBe('image/gif');
  });

  it('GIF89a → image/gif', async () => {
    const bytes = new TextEncoder().encode('GIF89a');
    await expect(detectImageMediaType(byteFile('a.gif', bytes, 'image/gif'))).resolves.toBe('image/gif');
  });

  it('JPEG SOI（FF D8 FF）→ image/jpeg', async () => {
    const bytes = Uint8Array.of(0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46);
    await expect(detectImageMediaType(byteFile('a.jpg', bytes, 'image/jpeg'))).resolves.toBe('image/jpeg');
  });

  it('RIFF…WEBP → image/webp', async () => {
    const bytes = Uint8Array.of(0x52, 0x49, 0x46, 0x46, 0x24, 0x00, 0x00, 0x00, 0x57, 0x45, 0x42, 0x50, 0x56, 0x50, 0x38, 0x20);
    await expect(detectImageMediaType(byteFile('a.webp', bytes, 'image/webp'))).resolves.toBe('image/webp');
  });

  it('纯文本字节 → null（改名骗不过字节）', async () => {
    const bytes = new TextEncoder().encode('hello plain text');
    await expect(detectImageMediaType(byteFile('a.png', bytes, 'image/png'))).resolves.toBeNull();
  });

  it('空文件 → null', async () => {
    await expect(detectImageMediaType(byteFile('empty.png', new Uint8Array(0), 'image/png'))).resolves.toBeNull();
  });

  it('2 字节截断文件 → null（不够任何一种签名的最短判据）', async () => {
    await expect(detectImageMediaType(byteFile('cut.png', Uint8Array.of(0x89, 0x50), 'image/png'))).resolves.toBeNull();
  });

  it('FF D8 00 → null（JPEG 判据是 3 字节 FF D8 FF，2 字节 SOI 不够）', async () => {
    const bytes = Uint8Array.of(0xff, 0xd8, 0x00, 0x10);
    await expect(detectImageMediaType(byteFile('a.jpg', bytes, 'image/jpeg'))).resolves.toBeNull();
  });

  it('RIFF + 非 WEBP（RIFF....XXXX）→ null（只认 RIFF 容器里的 WebP）', async () => {
    const bytes = Uint8Array.of(0x52, 0x49, 0x46, 0x46, 0x24, 0x00, 0x00, 0x00, 0x58, 0x58, 0x58, 0x58);
    await expect(detectImageMediaType(byteFile('a.riff', bytes, 'image/webp'))).resolves.toBeNull();
  });

  it('4 字节 GIF8 → null（GIF 判据是 6 字节 GIF87a/GIF89a）', async () => {
    const bytes = new TextEncoder().encode('GIF8');
    await expect(detectImageMediaType(byteFile('a.gif', bytes, 'image/gif'))).resolves.toBeNull();
  });
});

/**
 * #937 / M-20：`partitionIntake` 与探测结果的接线——map 里有条目就字节说了算
 * （null = 字节证伪），没有条目回退浏览器声明（兼容不探针的调用方）。
 */
describe('partitionIntake × probedTypes — #937 / M-20 接线', () => {
  const limits: ImageIntakeLimits = {
    maxImageBytes: 100,
    maxImagesPerMessage: 3,
    maxMessageImageBytes: 250,
    mediaTypes: ['image/png', 'image/jpeg', 'image/webp', 'image/gif'],
  };

  const fakePng = byteFile('a.png', new TextEncoder().encode('hello plain text'), 'image/png');
  const realPng = byteFile(
    'b.png',
    Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13),
    'image/png',
  );
  const gifBytes = byteFile('c.png', new TextEncoder().encode('GIF89a…'), 'image/png');

  it('声明 image/png 但 probed 给 null（字节是文本）→ 拒「不支持的图片格式」', () => {
    const out = partitionIntake([fakePng], [], limits, new Map([[fakePng, null]]));
    expect(out.accepted).toEqual([]);
    expect(out.error).toContain('不支持的图片格式');
    expect(out.error).toContain('a.png');
  });

  it('真实 PNG 字节（probed 给 image/png）→ accepted', () => {
    const out = partitionIntake([realPng], [], limits, new Map([[realPng, 'image/png']]));
    expect(out.error).toBeNull();
    expect(out.accepted).toEqual([realPng]);
  });

  it('声明 png、字节是 GIF（probed 给 image/gif）→ accepted：字节赢了且仍在允许集内', () => {
    const out = partitionIntake([gifBytes], [], limits, new Map([[gifBytes, 'image/gif']]));
    expect(out.error).toBeNull();
    expect(out.accepted).toEqual([gifBytes]);
  });

  it('不传 probedTypes → 老行为（浏览器声明生效）', () => {
    const out = partitionIntake([fakePng], [], limits);
    expect(out.error).toBeNull();
    expect(out.accepted).toEqual([fakePng]);
  });

  it('probed map 只有部分条目：有条目的按字节、没条目的回退声明', () => {
    const out = partitionIntake(
      [fakePng, realPng],
      [],
      limits,
      new Map([[fakePng, null]]), // realPng 不在 map 里 → 用声明的 image/png
    );
    expect(out.accepted).toEqual([]);
    expect(out.error).toContain('a.png');
    expect(out.error).not.toContain('b.png');
  });
});

describe('budgetText — AC2 的预算提示', () => {
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

  it('formatBytes 已收口到 ./format（M-22）：预算文案里的 MiB 由唯一实现渲染', () => {
    // 行为钉子：budgetText 的剩余字节串走 format.ts 的 IEC 实现（'200 MiB' 而非 '200.0 MB'）。
    const text = budgetText([]);
    expect(text).toContain('200 MiB');
  });
});

/** #937 / M-23：字节求和收口——此前 234/235/253 行三处手写 reduce 逐字重复。 */
describe('sumBytes — M-23 字节求和收口', () => {
  it('空数组 → 0', () => {
    expect(sumBytes([] as { bytes: number }[], (item) => item.bytes)).toBe(0);
  });

  it('混合条目逐项求和', () => {
    expect(sumBytes([{ bytes: 10 }, { bytes: 32 }], (item) => item.bytes)).toBe(42);
    expect(sumBytes([new File(['x'], 'a'), new File(['xx'], 'b')], (f) => f.size)).toBe(3);
  });
});

/**
 * #937 / M-08：上限从服务端下发（GET /api/attachments/limits），
 * `IMAGE_LIMITS` 退为离线 fallback。这里 mock 的是 `globalThis.fetch`——
 * `api.ts::getAttachmentLimits` 走 `apiFetch`（相对路径 fetch），
 * 在 node 环境下 stub 全局 fetch 即可覆盖整条链（含线上 snake_case → 内部
 * camelCase 的映射，`allowed_media_types` → `mediaTypes`）。
 */
describe('loadImageLimits / getImageLimits — #937 M-08 服务端下发', () => {
  /** 与 `IMAGE_LIMITS` **不同**的一组值——证明真的被服务端覆盖，而不是碰巧同值。 */
  const SERVER_LIMITS: ImageIntakeLimits = {
    maxImageBytes: 5 * 1024 * 1024,
    maxImagesPerMessage: 3,
    maxMessageImageBytes: 6 * 1024 * 1024,
    mediaTypes: ['image/avif'],
  };
  /** 后端 `app.py::get_attachment_limits` 的线上形状（snake_case wire 惯例）。 */
  const wireBody = {
    max_image_bytes: SERVER_LIMITS.maxImageBytes,
    max_images_per_message: SERVER_LIMITS.maxImagesPerMessage,
    max_message_image_bytes: SERVER_LIMITS.maxMessageImageBytes,
    allowed_media_types: [...SERVER_LIMITS.mediaTypes],
  };

  beforeEach(() => {
    setImageLimits(IMAGE_LIMITS); // 模块级状态在用例间复位
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('成功 → getImageLimits() 返回服务端的值（与默认值不同，证明真的被覆盖）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, json: async () => wireBody }) as Response),
    );
    const loaded = await loadImageLimits();
    expect(loaded).toEqual(SERVER_LIMITS);
    expect(getImageLimits()).toEqual(SERVER_LIMITS);
    expect(getImageLimits()).not.toEqual(IMAGE_LIMITS);
  });

  it('fetch 抛错 → 回落 IMAGE_LIMITS 且不抛（fallback 语义）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new Error('network down');
    }));
    await expect(loadImageLimits()).resolves.toEqual(IMAGE_LIMITS);
    expect(getImageLimits()).toEqual(IMAGE_LIMITS);
  });

  it('非 ok → 回落 IMAGE_LIMITS 且不抛', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 500, json: async () => ({}) }) as Response),
    );
    await expect(loadImageLimits()).resolves.toEqual(IMAGE_LIMITS);
    expect(getImageLimits()).toEqual(IMAGE_LIMITS);
  });

  it('先成功一次、再失败一次 → getImageLimits() 仍是服务端值（P4-2：失败不冲掉已拉到的值）', async () => {
    // 第一次成功拉到服务端值；第二次（token 变更后的补拉）网络失败。
    const fetchMock = vi.fn()
      .mockImplementationOnce(async () => ({ ok: true, json: async () => wireBody }) as Response)
      .mockImplementationOnce(async () => {
        throw new Error('network down');
      });
    vi.stubGlobal('fetch', fetchMock);
    await expect(loadImageLimits()).resolves.toEqual(SERVER_LIMITS);
    // 失败那次返回**调用前旧值**（= 已拉到的服务端值），而不是离线 fallback。
    await expect(loadImageLimits()).resolves.toEqual(SERVER_LIMITS);
    expect(getImageLimits()).toEqual(SERVER_LIMITS);
  });

  it('并发调用共用同一个 in-flight promise（fetch 只打一次）', async () => {
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => wireBody }) as Response);
    vi.stubGlobal('fetch', fetchMock);
    const [a, b] = await Promise.all([loadImageLimits(), loadImageLimits()]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(a).toEqual(SERVER_LIMITS);
    expect(b).toEqual(SERVER_LIMITS);
  });

  it('partitionIntake 默认参数吃刷新后的值（不传 limits 也按新上限拒绝）', async () => {
    setImageLimits({
      maxImageBytes: 50,
      maxImagesPerMessage: 3,
      maxMessageImageBytes: 250,
      mediaTypes: ['image/png'],
    });
    // 60 B > 刷新后的单张上限 50 B：默认参数路径必须拒绝（旧代码会按 20 MiB 放行）
    const out = partitionIntake([file('big.png', 'image/png', 60)]);
    expect(out.accepted).toEqual([]);
    expect(out.error).toContain('50 B');
  });
});
