/**
 * 粘贴图片**路径**的识别与读盘（AC2）。
 *
 * ADAPT 自 Cline（Apache-2.0，许可全文与来源见 `tui/THIRD_PARTY_NOTICES.md`）：
 * - `apps/cli/src/tui/utils/image-paste.ts:35-83` @ commit `cd80a20e96481f5f5d413789f6847accf846487b`
 *   （`normalizePasteText` / `stripWrappingQuotes` / `unescapeTerminalPath` /
 *   `resolvePastedImagePath`）；
 * - `apps/cli/src/utils/image-attachments.ts:4-29`（扩展名 -> 是否图片）。
 *
 * 与上游的三处差别（都在 NOTICE 里登记）：
 * 1. **砍掉 OpenTUI 依赖**：上游的 `PasteLikeEvent{bytes,metadata}` 来自 OpenTUI，
 *    `IMAGE_EXTENSIONS` 里的 bmp/svg 本仓服务端不收（`attachments/types.py::EXTENSION_MEDIA_TYPES`
 *    只有 png/jpg/jpeg/webp/gif）=> 扩展名集与服务端 **逐字对齐**；
 * 2. 上游用 `resolveExistingFilePath`（`@cline/shared/storage`，本仓无此包）做
 *    macOS 文件名变体归位；本仓 TUI 的粘贴路径只做"剥引号 / CRLF 归一 / `file://` 转换"，
 *    存在性由 `readImageFile` 真读盘判定（读不到就是明确错误，见 PRD 用户故事 24）；
 * 3. 上游把图读成 data URL（OpenTUI 内联渲染用）；本仓契约是**字节流式上传**
 *    （`POST /api/sessions/{id}/attachments`），故这里回字节而不是 base64。
 */
import { readFileSync } from "node:fs";
import { basename, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { detectSupportedImageMimeType } from "./mime.ts";
import type { PendingImage } from "./pending-images.ts";

/** 服务端 v1 接受的栅格图 media types（`attachments/types.py::SUPPORTED_IMAGE_MEDIA_TYPES`）。 */
export const SUPPORTED_IMAGE_MEDIA_TYPES: readonly string[] = [
  "image/png",
  "image/jpeg",
  "image/webp",
  "image/gif",
];

/** 扩展名 -> 是图片（与服务端 `EXTENSION_MEDIA_TYPES` 的键集逐字一致）。 */
const IMAGE_EXTENSIONS: readonly string[] = [".png", ".jpg", ".jpeg", ".webp", ".gif"];

/** 扩展名 -> media type（镜像服务端 `EXTENSION_MEDIA_TYPES`，用于声明一致性判定）。 */
const MEDIA_TYPE_BY_EXTENSION: Record<string, string> = {
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".gif": "image/gif",
};

/** 剪贴板字节的展示名（没有本地文件名）：扩展名必须与 media type 一致。 */
const CLIPBOARD_NAME_BY_MEDIA_TYPE: Record<string, string> = {
  "image/png": "clipboard.png",
  "image/jpeg": "clipboard.jpg",
  "image/webp": "clipboard.webp",
  "image/gif": "clipboard.gif",
};

/** 剪贴板取到的字节没有文件名：按 media type 给一个扩展名正确的展示名。 */
export function clipboardImageName(mimeType: string): string {
  return CLIPBOARD_NAME_BY_MEDIA_TYPE[mimeType] ?? "clipboard.png";
}

/**
 * 上传时的**声明名**（`?name=`）。
 *
 * 服务端会核对"文件名扩展名 vs 字节判定"，不一致直接拒（`IMAGE_TYPE_MISMATCH`，
 * `web/attachments.py:_check_declared_matches`）。所以扩展名与字节判定不符时**省略**
 * 声明名（服务端 `name` 可缺省），绝不谎报扩展名去换一个好看的名字。
 */
export function uploadDeclaredName(name: string, mimeType: string): string | undefined {
  const dot = name.lastIndexOf(".");
  const extension = dot >= 0 ? name.slice(dot).toLowerCase() : "";
  return MEDIA_TYPE_BY_EXTENSION[extension] === mimeType ? name : undefined;
}

export type ReadImageFileResult =
  | { ok: true; image: PendingImage }
  | { ok: false; reason: "missing" | "unsupported" };

export function isImagePath(filePath: string): boolean {
  const normalized = filePath.toLowerCase();
  return IMAGE_EXTENSIONS.some((extension) => normalized.endsWith(extension));
}

/**
 * 把一段**粘贴文本**解析成本地路径。
 *
 * 只接受**单行**：多行粘贴是文本内容，把它当路径会吞掉用户的话（上游同一纪律）。
 * 返回 `undefined` 表示"这不是一条路径"。
 */
export function resolvePastedImagePath(
  text: string,
  platform: NodeJS.Platform = process.platform,
): string | undefined {
  // \r\n / \r -> \n
  const normalized = text.replace(/\r\n/g, "\n").replace(/\r/g, "\n").trim();
  if (!normalized) {
    return undefined;
  }

  const lines = normalized.split("\n").filter((line) => line.trim().length > 0);
  if (lines.length !== 1) {
    return undefined;
  }

  const raw = (lines[0]?.trim() ?? "").replace(/^['"]+|['"]+$/g, "");
  if (!raw || /^(https?):\/\//i.test(raw)) {
    return undefined;
  }

  if (raw.startsWith("file://")) {
    try {
      return fileURLToPath(raw);
    } catch {
      return undefined;
    }
  }

  // 终端会把空格/特殊字符转义成 `\<char>`（非 Windows 才反转义：Windows 路径里的
  // 反斜杠是分隔符，反转义会毁掉整条路径）。
  return platform === "win32" ? raw : raw.replace(/\\(.)/g, "$1");
}

/**
 * 粘贴文本是否是一条**图片文件路径** => 返回路径，否则 `null`。
 * 纯判定（不碰磁盘）：AC2 的"自动识别为附图"入口。
 */
export function pastedImagePath(
  text: string,
  platform: NodeJS.Platform = process.platform,
): string | null {
  const filePath = resolvePastedImagePath(text, platform);
  if (filePath === undefined || !isImagePath(filePath)) {
    return null;
  }
  return filePath;
}

/**
 * 读一张图片文件：`ok:false` 给出**明确原因**（`missing` 文件不在 / 读不了，
 * `unsupported` 按 magic bytes 判不是本仓接受的图片），调用方据此给用户可读错误。
 */
export function readImageFile(filePath: string): ReadImageFileResult {
  let bytes: Buffer;
  try {
    bytes = readFileSync(filePath);
  } catch {
    return { ok: false, reason: "missing" };
  }
  const mimeType = detectSupportedImageMimeType(bytes);
  if (mimeType === null || !SUPPORTED_IMAGE_MEDIA_TYPES.includes(mimeType)) {
    return { ok: false, reason: "unsupported" };
  }
  const absolute = resolve(filePath);
  return {
    ok: true,
    image: {
      bytes: new Uint8Array(bytes),
      mimeType,
      name: basename(absolute),
      path: absolute,
    },
  };
}
