/**
 * M-10 跨端快照守卫（#938）：web 离线 fallback 的 media type 集 vs 服务端权威源快照。
 *
 * 快照 `src/agent_harness/attachments/image_media_types.snapshot.txt` 由
 * `tests/attachments/test_image_media_types_snapshot.py` 从 `types.py` 权威表钉住；
 * 本文件钉住"`IMAGE_LIMITS.mediaTypes` == 快照"——web 侧单方面增删类型就红。
 * 对账做法 ADAPT 自本仓 `tests/web/test_attachment_limits_contract.py`（python 读
 * 对端源逐字对账）与 `tui/test/host.test.ts`（`new URL(...)` 读仓库文件）先例。
 */
import { readFileSync } from 'node:fs';

import { expect, it } from 'vitest';

import { IMAGE_LIMITS } from './attachments';

const SNAPSHOT = new URL(
  '../../../src/agent_harness/attachments/image_media_types.snapshot.txt',
  import.meta.url,
);

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
  const snapshot = parseMediaTypes(readFileSync(SNAPSHOT, 'utf8'));
  expect([...IMAGE_LIMITS.mediaTypes].sort()).toEqual(snapshot);
});
