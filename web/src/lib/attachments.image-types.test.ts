/**
 * M-10 跨端快照守卫（#938）：web 离线 fallback 的 media type 集 vs 服务端权威源快照。
 *
 * 快照 `src/agent_harness/attachments/image_media_types.snapshot.txt` 由
 * `tests/attachments/test_image_media_types_snapshot.py` 从 `types.py` 权威表钉住；
 * 本文件钉住"`IMAGE_LIMITS.mediaTypes` == 快照"——web 侧单方面增删类型就红。
 * 对账做法 ADAPT 自本仓 `tests/web/test_attachment_limits_contract.py`（python 读
 * 对端源逐字对账）先例。
 *
 * 用 Vite 原生 `?raw` 导入而不用 `node:fs` 读文件：app tsconfig 的 `types` 刻意只有
 * `["vite/client"]`（无 node 类型，TS2591），而 `vite/client` 已声明 `*?raw` 模块，
 * `tsc -b` 可过。
 */
import snapshotText from '../../../src/agent_harness/attachments/image_media_types.snapshot.txt?raw';

import { expect, it } from 'vitest';

import { detectImageMediaType, IMAGE_LIMITS } from './attachments';

/** 解析快照 [media_types] 一节（跳过 `#` 注释与空行）。 */
function parseMediaTypes(source: string): string[] {
  const mediaTypes: string[] = [];
  let inSection = false;
  for (const line of source.split('\n')) {
    const trimmed = line.trim();
    if (trimmed === '' || trimmed.startsWith('#')) continue;
    if (trimmed.startsWith('[')) {
      inSection = trimmed === '[media_types]';
      continue;
    }
    if (inSection) mediaTypes.push(trimmed);
  }
  return mediaTypes;
}

it('IMAGE_LIMITS.mediaTypes 与快照 media_types 一致（#938 M-10）', () => {
  const snapshot = parseMediaTypes(snapshotText);
  expect([...IMAGE_LIMITS.mediaTypes].sort()).toEqual(snapshot);
});

// Correctness P3-1（#938 修回）跟进：`detectImageMediaType` 的魔数字节签名表是事实上的
// 第四张图片类型表——若未来权威源新增类型（如 image/avif）而这里没跟，全门禁绿但对应
// 字节会被静默判 null。本测试钉住「字节探测能识别的类型集 == 权威源集」。
it('detectImageMediaType 能按魔数识别的媒体类型集与快照 media_types 一致', async () => {
  const cases: Array<[string, Uint8Array<ArrayBuffer>, string]> = [
    ['x.png', Uint8Array.of(0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a), 'image/png'],
    ['x.gif', new TextEncoder().encode('GIF89a'), 'image/gif'],
    ['x.gif', new TextEncoder().encode('GIF87a'), 'image/gif'],
    ['x.jpg', Uint8Array.of(0xff, 0xd8, 0xff), 'image/jpeg'],
    ['x.webp', Uint8Array.of(0x52, 0x49, 0x46, 0x46, 0x00, 0x00, 0x00, 0x00, 0x57, 0x45, 0x42, 0x50), 'image/webp'],
  ];
  const seen = new Set<string>();
  for (const [name, bytes, expected] of cases) {
    // type 传 ''：证明判定完全由字节（而非浏览器声明）说了算。
    const file = new File([bytes], name, { type: '' });
    await expect(detectImageMediaType(file)).resolves.toBe(expected);
    seen.add(expected);
  }
  const snapshot = parseMediaTypes(snapshotText);
  expect([...seen].sort()).toEqual(snapshot);
});
