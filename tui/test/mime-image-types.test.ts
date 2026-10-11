/**
 * 图片类型识别集守卫（#983，MM 暴力测试 Correctness B3-1）。
 *
 * `src/lib/mime.ts::detectSupportedImageMimeType` 的 magic-bytes 签名表是事实上的
 * 第 4 份图片类型副本；#938 的三套守卫只对账 `image-paste.ts` 三表，对它零覆盖。
 * 本文件钉住「字节探测能识别的类型集 == 服务端权威表」：
 * 权威表 `src/agent_harness/attachments/validation.py::SUPPORTED_IMAGE_MEDIA_TYPES`
 * （来源: DSH `attachment/src/types.ts` `ImageMediaType`），canonical 化为快照 txt。
 *
 * 对账形状 ADAPT 自 web 侧同构守卫
 * `web/src/lib/attachments.image-types.test.ts:44-62`（Correctness P3-1，#938 修回），
 * 改写为 node:test + 直接喂 Uint8Array 样本；快照读取走 #938 既有惯例
 * （`tui/test/image-media-types-snapshot.test.ts` 的 URL + readFileSync）。
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { detectSupportedImageMimeType } from "../src/lib/mime.ts";

const SNAPSHOT = new URL(
  "../../src/agent_harness/attachments/image_media_types.snapshot.txt",
  import.meta.url,
);

/** 解析快照 [media_types] 一节（跳过 `#` 注释与空行）。 */
function parseMediaTypes(source: string): string[] {
  const mediaTypes: string[] = [];
  let inSection = false;
  for (const line of source.split("\n")) {
    const trimmed = line.trim();
    if (trimmed === "" || trimmed.startsWith("#")) continue;
    if (trimmed.startsWith("[")) {
      inSection = trimmed === "[media_types]";
      continue;
    }
    if (inSection) mediaTypes.push(trimmed);
  }
  return mediaTypes;
}

/** 最小合法 PNG：签名 + IHDR(len=13, "IHDR") + IDAT 收尾（mime.ts 要求 IHDR 校验）。 */
function pngBytes(): Uint8Array {
  const signature = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
  const ihdr = [
    0x00, 0x00, 0x00, 0x0d, // length = 13
    0x49, 0x48, 0x44, 0x52, // "IHDR"
    0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00,
    0x1f, 0x15, 0xc4, 0x89, // crc
    0x00, 0x00, 0x00, 0x00, // IDAT length = 0
    0x49, 0x44, 0x41, 0x54, // "IDAT"
    0xae, 0x42, 0x60, 0x82, // crc
  ];
  return Uint8Array.from([...signature, ...ihdr]);
}

/** 最小合法 BMP 头（BITMAPINFOHEADER，24bpp），足以通过 mime.ts 原有的 BMP 判定。 */
function bmpBytes(): Uint8Array {
  const bytes = new Uint8Array(30);
  const ascii = (offset: number, text: string): void => {
    for (let index = 0; index < text.length; index++) {
      bytes[offset + index] = text.charCodeAt(index);
    }
  };
  const u16le = (offset: number, value: number): void => {
    bytes[offset] = value & 0xff;
    bytes[offset + 1] = (value >> 8) & 0xff;
  };
  const u32le = (offset: number, value: number): void => {
    bytes[offset] = value & 0xff;
    bytes[offset + 1] = (value >> 8) & 0xff;
    bytes[offset + 2] = (value >> 16) & 0xff;
    bytes[offset + 3] = (value >>> 24) & 0xff;
  };
  ascii(0, "BM");
  u32le(2, 0); // declaredFileSize 未知（0），跳过该检查
  u32le(10, 54); // pixelDataOffset = 14 + 40（DIB 头）
  u32le(14, 40); // dibHeaderSize = BITMAPINFOHEADER
  u32le(18, 1); // width
  u32le(22, 1); // height
  u16le(26, 1); // colorPlanes = 1
  u16le(28, 24); // bitsPerPixel = 24
  return bytes;
}

test("detectSupportedImageMimeType 按魔数识别的媒体类型集与快照 media_types 一致（#983）", () => {
  const cases: Array<[string, Uint8Array, string]> = [
    ["png", pngBytes(), "image/png"],
    ["gif87a", new TextEncoder().encode("GIF87a"), "image/gif"],
    ["gif89a", new TextEncoder().encode("GIF89a"), "image/gif"],
    ["jpeg", Uint8Array.of(0xff, 0xd8, 0xff, 0xe0), "image/jpeg"],
    [
      "webp",
      Uint8Array.of(0x52, 0x49, 0x46, 0x46, 0x00, 0x00, 0x00, 0x00, 0x57, 0x45, 0x42, 0x50),
      "image/webp",
    ],
  ];
  const seen = new Set<string>();
  for (const [label, bytes, expected] of cases) {
    assert.equal(detectSupportedImageMimeType(bytes), expected, label);
    seen.add(expected);
  }
  const snapshot = parseMediaTypes(readFileSync(SNAPSHOT, "utf8"));
  assert.deepEqual([...seen].sort(), snapshot);
});

// #983：bmp 不在权威表（validation.py 无 image/bmp），且两条生产路径
// （image-paste.ts 的 SUPPORTED_IMAGE_MEDIA_TYPES 过滤、clipboard-image.ts 的
// isSupportedImageMimeType）本就把它拒掉。mime.ts 的 bmp 分支删除后，
// BMP 字节必须返回 null，而不是识别出权威表之外的类型。
test("BMP 字节返回 null（权威表外类型不得被识别，#983）", () => {
  assert.equal(detectSupportedImageMimeType(bmpBytes()), null);
});
