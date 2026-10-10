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
import { mkdtempSync, rmSync, symlinkSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { test } from "node:test";

import {
  checkMessageImageLimits,
  clipboardImageName,
  isImagePath,
  MAX_IMAGES_PER_MESSAGE,
  MAX_MESSAGE_IMAGE_BYTES,
  MAX_IMAGE_BYTES,
  pastedImagePath,
  readImageFile,
  resolvePastedImagePath,
  uploadDeclaredName,
} from "../src/lib/image-paste.ts";
import { PNG_BYTES as PNG } from "./fixtures.ts";

/** 命令不存在时 `child_process` 抛出的错误带 `code = "ENOENT"`（`Error` 上没声明这个字段）。 */
function isEnoent(error: unknown): boolean {
  if (!(error instanceof Error) || !("code" in error)) return false;
  return error.code === "ENOENT";
}

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

test("file:// URL 转成本地路径（按注入的 platform 判定，不吃运行时平台）", () => {
  assert.equal(resolvePastedImagePath("file:///tmp/shot.png", "linux"), "/tmp/shot.png");
  // 同一份代码在 Windows 运行时上：注入 win32 必须走盘符语义，注入 linux 必须走 POSIX
  // 语义（#830 D3：`fileURLToPath` 原先只看运行时平台 => Windows 上这条必红）。
  assert.equal(
    resolvePastedImagePath("file:///C:/shots/a.png", "win32"),
    "C:\\shots\\a.png",
  );
  assert.equal(resolvePastedImagePath("file:///tmp/shot.png", "win32"), undefined);
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

test("readImageFile：非常规文件按 missing 明确拒绝，且绝不阻塞（P4：FIFO 上的 readFileSync 会永久卡住）", (t) => {
  const dir = mkdtempSync(join(tmpdir(), "ia827-fifo-"));
  try {
    const fifo = join(dir, "pipe.png");
    try {
      execFileSync("mkfifo", [fifo]);
    } catch (error) {
      // `mkfifo` 是外部 POSIX 二进制：Windows 上只有 runner 镜像 PATH 里的 Git for Windows
      // coreutils 提供它（复审 P3）⇒ 环境缺它时**显式跳过**，别把它当成产品回归。
      if (!isEnoent(error)) throw error;
      t.skip("本机没有 mkfifo（POSIX FIFO 用例）");
      return;
    }
    const started = Date.now();
    assert.deepEqual(readImageFile(fifo), { ok: false, reason: "missing" });
    assert.ok(Date.now() - started < 2_000, "不得阻塞在读盘上（无写端的 FIFO 会一直等）");
    assert.deepEqual(readImageFile(dir), { ok: false, reason: "missing" }, "目录同样按 missing 拒绝");
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});

/**
 * symlink 是**宿主能力**：Windows 未开开发者模式/非管理员时 `symlinkSync` 直接 `EPERM`。
 * 探针失败按能力跳过（runner 与 Linux 上探针通过、真跑本用例），不把环境差异判成产品红。
 */
function symlinkSkipReason(): false | string {
  const dir = mkdtempSync(join(tmpdir(), "ia827-link-probe-"));
  try {
    const target = join(dir, "shot.png");
    writeFileSync(target, PNG);
    symlinkSync(target, join(dir, "link.png"));
    return false;
  } catch {
    return "宿主不允许创建符号链接（Windows 需开发者模式或管理员），按能力探测跳过";
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

test(
  "readImageFile：符号链接指向常规图片照常可读（常规文件守卫不误伤链接）",
  { skip: symlinkSkipReason() },
  () => {
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
  },
);

test("readImageFile：超过 20 MiB 上限 ⇒ too_large，且不把整文件读进内存", () => {
  const dir = mkdtempSync(join(tmpdir(), "ia827-big-"));
  try {
    const file = join(dir, "huge.png");
    // 稀疏文件：只占 inode 元数据，不真写 21 MiB 数据。用 Node 原生 `truncateSync`（先建空文件：
    // 它不像外部 `truncate -s` 那样自带创建）——外部 `truncate` 二进制在 Windows 上只有 runner
    // 镜像 PATH 里的 Git for Windows coreutils 提供（复审 P3：那属外部依赖，不该进用例）。
    writeFileSync(file, "");
    truncateSync(file, 21 * 1024 * 1024);
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
    writeFileSync(file, "");
    truncateSync(file, MAX_IMAGE_BYTES);
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

test("M-21：单条数量/总量上限与服务端 config.py 默认值同值（镜像口径，漂移 = 预检放行、服务端 413/422）", () => {
  // 与 web/src/lib/attachments.ts 的 IMAGE_LIMITS 同一规则、同一默认（PRD D11：
  // 后端权威、前端镜像；#824 明确不加 GET limits 端点）。改动必须三处同步。
  assert.equal(MAX_IMAGES_PER_MESSAGE, 20);
  assert.equal(MAX_MESSAGE_IMAGE_BYTES, 200 * 1024 * 1024);
  // 单张上限沿用既有 MAX_IMAGE_BYTES 镜像（attachment_max_image_bytes 默认 20 MiB）。
  assert.equal(MAX_IMAGE_BYTES, 20 * 1024 * 1024);
});

test("M-21：checkMessageImageLimits 数量超限（第 21 张被拒，理由含上限与出路）", () => {
  const reason = checkMessageImageLimits(20, 0, 95);
  assert.ok(reason !== null);
  assert.ok(reason.includes("最多 20 张"));
});

test("M-21：checkMessageImageLimits 总字节超限（已附 + 本批 一起算）", () => {
  // 已附 190 MiB + 本批 20 MiB > 200 MiB：即便没到 20 张也拒。
  const mib = 1024 * 1024;
  const reason = checkMessageImageLimits(2, 190 * mib, 20 * mib);
  assert.ok(reason !== null);
  assert.ok(reason.includes("200 MiB"));
  // 恰好压线（不超）放行：199 + 1 = 200 MiB。
  assert.equal(checkMessageImageLimits(2, 199 * mib, 1 * mib), null);
});

test("M-21：checkMessageImageLimits 未超限 => null（放行）", () => {
  assert.equal(checkMessageImageLimits(0, 0, 95), null);
  assert.equal(checkMessageImageLimits(19, 199 * 1024 * 1024, 1024), null);
});
