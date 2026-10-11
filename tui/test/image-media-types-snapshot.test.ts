/**
 * M-10 跨端快照守卫（#938）：TUI 的图片类型三表 vs 服务端权威源快照。
 *
 * 快照 `src/agent_harness/attachments/image_media_types.snapshot.txt` 由
 * `tests/attachments/test_image_media_types_snapshot.py` 从
 * `types.py::SUPPORTED_IMAGE_MEDIA_TYPES` / `EXTENSION_MEDIA_TYPES` 钉住；
 * 本文件再钉住"TUI 侧三表 == 快照"：任何一侧单方面改动都会红。
 * 读仓库文件的先例：`tui/test/host.test.ts`（`new URL("../../src/...", import.meta.url)`）。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  CLIPBOARD_NAME_BY_MEDIA_TYPE,
  IMAGE_EXTENSIONS,
  MEDIA_TYPE_BY_EXTENSION,
  SUPPORTED_IMAGE_MEDIA_TYPES,
} from "../src/lib/image-paste.ts";

const SNAPSHOT = new URL(
  "../../src/agent_harness/attachments/image_media_types.snapshot.txt",
  import.meta.url,
);

/** 解析快照的两节（[media_types] / [extension_media_types]），跳过 `#` 注释。 */
function parseSnapshot(source: string): { mediaTypes: string[]; extensionMediaTypes: [string, string][] } {
  const mediaTypes: string[] = [];
  const extensionMediaTypes: [string, string][] = [];
  let section: string | null = null;
  for (const line of source.split("\n")) {
    const trimmed = line.trim();
    if (trimmed === "" || trimmed.startsWith("#")) continue;
    if (trimmed.startsWith("[")) {
      section = trimmed.slice(1, -1);
      continue;
    }
    if (section === "media_types") {
      mediaTypes.push(trimmed);
    } else if (section === "extension_media_types") {
      const eq = trimmed.indexOf("=");
      assert.ok(eq > 0, "快照 extension_media_types 行缺等号：" + trimmed);
      extensionMediaTypes.push([trimmed.slice(0, eq), trimmed.slice(eq + 1)]);
    } else {
      assert.fail(`快照出现未知小节（${String(section)}）的行：${trimmed}`);
    }
  }
  return { mediaTypes, extensionMediaTypes };
}

test("SUPPORTED_IMAGE_MEDIA_TYPES 与快照 media_types 一致", () => {
  const snapshot = parseSnapshot(readFileSync(SNAPSHOT, "utf8"));
  assert.deepEqual([...SUPPORTED_IMAGE_MEDIA_TYPES].sort(), snapshot.mediaTypes);
});

test("MEDIA_TYPE_BY_EXTENSION 的条目集合与快照 extension_media_types 一致", () => {
  const snapshot = parseSnapshot(readFileSync(SNAPSHOT, "utf8"));
  const actual = Object.entries(MEDIA_TYPE_BY_EXTENSION).sort(([a], [b]) => a.localeCompare(b));
  assert.deepEqual(actual, snapshot.extensionMediaTypes);
});

test("IMAGE_EXTENSIONS 与快照的扩展名集合一致", () => {
  const snapshot = parseSnapshot(readFileSync(SNAPSHOT, "utf8"));
  const expected = snapshot.extensionMediaTypes.map(([extension]) => extension).sort();
  assert.deepEqual([...IMAGE_EXTENSIONS].sort(), expected);
});

test("CLIPBOARD_NAME_BY_MEDIA_TYPE 的键集与快照 media_types 一致", () => {
  const snapshot = parseSnapshot(readFileSync(SNAPSHOT, "utf8"));
  assert.deepEqual(Object.keys(CLIPBOARD_NAME_BY_MEDIA_TYPE).sort(), snapshot.mediaTypes);
});
