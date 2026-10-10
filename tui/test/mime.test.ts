/**
 * magic-bytes MIME 探测（#827 MM-06 / AC2）。
 *
 * 只断言外部可观察行为：给一段字节，回一个 media type 或 null。签名表来自
 * Pi `packages/coding-agent/src/utils/mime.ts` @ `28dcce2b`（COPY，见文件头）。
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { detectSupportedImageMimeType } from "../src/lib/mime.ts";

/** 最小合法 PNG：签名 + IHDR(len=13, "IHDR") + 若干块。 */
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

test("JPEG（ff d8 ff，非 F7 变体）", () => {
  assert.equal(detectSupportedImageMimeType(Uint8Array.from([0xff, 0xd8, 0xff, 0xe0])), "image/jpeg");
});

test("JPEG F7 变体（jpeg-lossless）被拒，不当成 jpeg", () => {
  assert.equal(detectSupportedImageMimeType(Uint8Array.from([0xff, 0xd8, 0xff, 0xf7])), null);
});

test("PNG 签名 + IHDR(len=13) ⇒ image/png", () => {
  assert.equal(detectSupportedImageMimeType(pngBytes()), "image/png");
});

test("PNG 签名但 IHDR 长度不对 ⇒ 拒绝（不猜）", () => {
  const broken = pngBytes();
  broken[8] = 0x01;
  assert.equal(detectSupportedImageMimeType(broken), null);
});

test("GIF87a / GIF89a / WEBP(RIFF) 各自识别", () => {
  const ascii = (text: string): number[] => [...text].map((c) => c.charCodeAt(0));
  assert.equal(detectSupportedImageMimeType(Uint8Array.from(ascii("GIF87a"))), "image/gif");
  assert.equal(detectSupportedImageMimeType(Uint8Array.from(ascii("GIF89a"))), "image/gif");
  assert.equal(
    detectSupportedImageMimeType(Uint8Array.from([...ascii("RIFF"), 0, 0, 0, 0, ...ascii("WEBP")])),
    "image/webp",
  );
});

test("非图片字节（纯文本）⇒ null", () => {
  assert.equal(detectSupportedImageMimeType(new TextEncoder().encode("hello world")), null);
});

test("空缓冲 ⇒ null（不抛）", () => {
  assert.equal(detectSupportedImageMimeType(new Uint8Array()), null);
});
