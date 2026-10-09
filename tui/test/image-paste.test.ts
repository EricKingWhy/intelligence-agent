/**
 * 粘贴的图片**路径**自动识别为附图（#827 MM-06 / AC2）。
 *
 * ADAPT 自 Cline `apps/cli/src/tui/utils/image-paste.ts` 与
 * `apps/cli/src/utils/image-attachments.ts` @ `cd80a20e`（Apache-2.0，见文件头）。
 * 两处刻意的本仓偏离（都写在 `tui/THIRD_PARTY_NOTICES.md`）：
 * ① 换掉 OpenTUI 的 `PasteEvent` 类型与 `@cline/shared` 的 `resolveExistingFilePath`；
 * ② 扩展名集与服务端 `attachments/types.py::EXTENSION_MEDIA_TYPES` 对齐（png/jpg/jpeg/
 *    webp/gif），不含上游的 bmp/svg —— 服务端 v1 只收这四种栅格图。
 */
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import {
  clipboardImageName,
  isImagePath,
  MAX_IMAGE_BYTES,
  pastedImagePath,
  readImageFile,
  resolvePastedImagePath,
  uploadDeclaredName,
} from "../src/lib/image-paste.ts";

const PNG = Uint8Array.from([
  0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
  0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
  0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x06, 0x00, 0x00, 0x00,
  0x1f, 0x15, 0xc4, 0x89,
  0x00, 0x00, 0x00, 0x00, 0x49, 0x44, 0x41, 0x54, 0xae, 0x42, 0x60, 0x82,
]);

test("剥掉包裹引号（Windows 复制路径常带引号）", () => {
  assert.equal(resolvePastedImagePath('"/tmp/shot.png"', "linux"), "/tmp/shot.png");
  assert.equal(resolvePastedImagePath("'/tmp/shot.png'", "linux"), "/tmp/shot.png");
});

test("CRLF/CR 归一到单行后接受", () => {
  assert.equal(resolvePastedImagePath("/tmp/shot.png\r\n", "linux"), "/tmp/shot.png");
  assert.equal(resolvePastedImagePath("/tmp/shot.png\r", "linux"), "/tmp/shot.png");
});

test("多行粘贴不当作路径（绝不吞掉一段文本）", () => {
  assert.equal(resolvePastedImagePath("/tmp/a.png\n/tmp/b.png", "linux"), undefined);
  assert.equal(resolvePastedImagePath("看看这个\n/tmp/a.png", "linux"), undefined);
});

test("空文本 ⇒ undefined", () => {
  assert.equal(resolvePastedImagePath("   \n ", "linux"), undefined);
});

test("http(s) URL 不是本地路径 ⇒ undefined", () => {
  assert.equal(resolvePastedImagePath("https://example.com/a.png", "linux"), undefined);
});

test("file:// URL 转成本地路径", () => {
  assert.equal(resolvePastedImagePath("file:///tmp/shot.png", "linux"), "/tmp/shot.png");
});

test("非 win32 反转义终端插入的反斜杠；win32 保留原样", () => {
  assert.equal(resolvePastedImagePath("/tmp/my\\ shot.png", "linux"), "/tmp/my shot.png");
  assert.equal(resolvePastedImagePath("C:\\shots\\a.png", "win32"), "C:\\shots\\a.png");
});

test("扩展名：只认服务端 v1 收的四种栅格图", () => {
  for (const good of ["a.png", "a.JPG", "a.jpeg", "a.webp", "a.gif"]) {
    assert.equal(isImagePath(good), true, `${good} 应被识别为图片`);
  }
  for (const bad of ["a.bmp", "a.svg", "a.txt", "a"]) {
    assert.equal(isImagePath(bad), false, `${bad} 不应被识别为图片`);
  }
});

test("pastedImagePath：路径 + 图片扩展名同时成立才算附图", () => {
  assert.equal(pastedImagePath('"/tmp/shot.png"', "linux"), "/tmp/shot.png");
  assert.equal(pastedImagePath("/tmp/notes.txt", "linux"), null);
});

test("readImageFile：真文件按 magic bytes 判型，回绝对路径与文件名", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia-tui-paste-"));
  try {
    const file = join(dir, "shot.png");
    writeFileSync(file, PNG);
    const result = readImageFile(file);
    assert.equal(result.ok, true);
    assert.ok(result.ok);
    assert.equal(result.image.mimeType, "image/png");
    assert.equal(result.image.name, "shot.png");
    assert.equal(result.image.path, resolve(file));
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readImageFile：文件不存在 ⇒ missing（明确错误，不发坏消息）", () => {
  const result = readImageFile(join(tmpdir(), "ia-tui-not-there-9f2c.png"));
  assert.deepEqual(result, { ok: false, reason: "missing" });
});

test("readImageFile：扩展名骗人（.png 里是文本）⇒ unsupported（按字节判，不按名字）", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia-tui-paste-"));
  try {
    const file = join(dir, "liar.png");
    writeFileSync(file, "这不是图片");
    assert.deepEqual(readImageFile(file), { ok: false, reason: "unsupported" });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readImageFile：服务端不收的格式（BMP）⇒ unsupported", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia-tui-paste-"));
  try {
    const file = join(dir, "old.bmp");
    writeFileSync(
      file,
      Uint8Array.from([
        0x42, 0x4d, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x36, 0x00, 0x00, 0x00,
        0x28, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x00,
        0x18, 0x00,
      ]),
    );
    assert.deepEqual(readImageFile(file), { ok: false, reason: "unsupported" });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("uploadDeclaredName：扩展名与字节判定一致才声明（否则省略，不谎报）", () => {
  assert.equal(uploadDeclaredName("shot.png", "image/png"), "shot.png");
  assert.equal(uploadDeclaredName("shot.JPEG", "image/jpeg"), "shot.JPEG");
  assert.equal(
    uploadDeclaredName("shot.png", "image/jpeg"),
    undefined,
    "PNG 名字 + JPEG 字节 ⇒ 服务端会判 IMAGE_TYPE_MISMATCH，宁可不声明",
  );
  assert.equal(uploadDeclaredName("noextension", "image/png"), undefined);
});

test("clipboardImageName：剪贴板文件名按 media type 给，扩展名与字节判定一致", () => {
  for (const mimeType of ["image/png", "image/jpeg", "image/webp", "image/gif"]) {
    assert.equal(uploadDeclaredName(clipboardImageName(mimeType), mimeType), clipboardImageName(mimeType));
  }
});

test("readImageFile：非常规文件按 missing 明确拒绝，且绝不阻塞（P4：FIFO 上的 readFileSync 会永久卡住）", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia827-fifo-"));
  try {
    const fifo = join(dir, "pipe.png");
    execFileSync("mkfifo", [fifo]);
    const started = Date.now();
    assert.deepEqual(readImageFile(fifo), { ok: false, reason: "missing" });
    assert.ok(Date.now() - started < 2_000, "不得阻塞在读盘上（无写端的 FIFO 会一直等）");
    assert.deepEqual(readImageFile(dir), { ok: false, reason: "missing" }, "目录同样按 missing 拒绝");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readImageFile：符号链接指向常规图片照常可读（常规文件守卫不误伤链接）", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia827-link-"));
  try {
    const target = join(dir, "shot.png");
    writeFileSync(target, PNG);
    const link = join(dir, "link.png");
    symlinkSync(target, link);
    const result = readImageFile(link);
    assert.equal(result.ok, true);
    if (result.ok) {
      assert.equal(result.image.mimeType, "image/png");
      assert.equal(result.image.name, "link.png");
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readImageFile：超过 20 MiB 上限 ⇒ too_large，且不把整文件读进内存", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia827-big-"));
  try {
    const file = join(dir, "huge.png");
    // 稀疏文件：只占 inode 元数据，不真写 21 MiB 数据。
    execFileSync("truncate", ["-s", "21M", file]);
    const started = Date.now();
    // 必须是 too_large 而不是 unsupported：尺寸门在 magic-bytes 判型**之前**短路
    // （全零的稀疏文件若被读进内存判型，只会得到 unsupported）。
    assert.deepEqual(readImageFile(file), { ok: false, reason: "too_large" });
    assert.ok(Date.now() - started < 1_000, "超限必须在读盘之前拒掉");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test("readImageFile：恰好等于上限不拒（口径 = 服务端 attachment_max_image_bytes 的严格大于）", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia827-limit-"));
  try {
    const file = join(dir, "limit.png");
    execFileSync("truncate", ["-s", String(MAX_IMAGE_BYTES), file]);
    // 20 MiB 全零文件：过尺寸门 ⇒ 按 magic bytes 判型 ⇒ unsupported（不是 too_large）。
    assert.deepEqual(readImageFile(file), { ok: false, reason: "unsupported" });
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

test(
  "readImageFile：字符设备（/dev/zero）按 missing 快速拒绝，绝不阻塞",
  { skip: process.platform === "win32" },
  () => {
    const started = Date.now();
    assert.deepEqual(readImageFile("/dev/zero"), { ok: false, reason: "missing" });
    assert.ok(Date.now() - started < 2_000, "字符设备不得被读盘阻塞");
  },
);
