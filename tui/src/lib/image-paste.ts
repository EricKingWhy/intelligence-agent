/**
 * 粘贴图片**路径**的识别与读盘（AC2）。
 *
 * ADAPT 自 Cline（Apache-2.0，许可全文与来源见 `tui/THIRD_PARTY_NOTICES.md`）：
 * - `apps/cli/src/tui/utils/image-paste.ts:35-83` @ commit `cd80a20e96481f5f5d413789f6847accf846487b`
 *   （`normalizePasteText` / `stripWrappingQuotes` / `unescapeTerminalPath` /
 *   `resolvePastedImagePath`）；
 * - `apps/cli/src/utils/image-attachments.ts:4-29`（扩展名 -> 是否图片）。
 *
 * 与上游的四处差别（都在 NOTICE 里登记）：
 * 1. **砍掉 OpenTUI 依赖**：上游的 `PasteLikeEvent{bytes,metadata}` 来自 OpenTUI，
 *    `IMAGE_EXTENSIONS` 里的 bmp/svg 本仓服务端不收（`attachments/types.py::EXTENSION_MEDIA_TYPES`
 *    只有 png/jpg/jpeg/webp/gif）=> 扩展名集与服务端 **逐字对齐**；
 * 2. 上游用 `resolveExistingFilePath`（`@cline/shared/storage`，本仓无此包）做
 *    macOS 文件名变体归位；本仓 TUI 的粘贴路径只做"剥引号 / CRLF 归一 / `file://` 转换"，
 *    存在性由 `readImageFile` 真读盘判定（读不到就是明确错误，见 PRD 用户故事 24）；
 * 3. 上游把图读成 data URL（OpenTUI 内联渲染用）；本仓契约是**字节流式上传**
 *    （`POST /api/sessions/{id}/attachments`），故这里回字节而不是 base64；
 * 4. 上游 `readImageDataUrlFromPastedText` 无条件 `readFileSync`；本仓读盘前先
 *    `statSync(...).isFile()` 筛查，再把**同一 fd** 交给 `fstatSync` 判定：非常规文件
 *    （FIFO / 设备 / 目录）一律回 `missing`，否则无写端的 FIFO 会让阻塞式读盘**永久**
 *    冻住整个 TUI 事件循环（`openSync` 落在 FIFO 上同样会阻塞，故这条 `statSync` 筛查
 *    不能省）。同一 fd 上还做**大小上限**预检（与服务端 `attachment_max_image_bytes`
 *    同口径），超限直接拒，不把整文件读进内存。
 */
import { closeSync, fstatSync, openSync, readFileSync, statSync } from "node:fs";
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
  | { ok: false; reason: "missing" | "unsupported" | "too_large" };

/**
 * 单张图片的本地尺寸上限：与服务端**同源口径**（`src/agent_harness/config.py:166`
 * `attachment_max_image_bytes` 默认 `20 * 1024 * 1024`）。本地先拒，省掉一次注定 413
 * 的上传，也不把超大文件读进内存。
 */
export const MAX_IMAGE_BYTES = 20 * 1024 * 1024;

/**
 * M-21：单条消息图片**数量**上限：镜像服务端 `config.py:167`
 * `attachment_max_images_per_message` 默认 20（PRD D11「后端权威、前端镜像」；
 * #824 明确不加 GET limits 端点，故无下发通道）。漂移的后果是「预检放行、服务端
 * 413/422」-- 消息仍会被权威拒绝，不会静默出错。web 侧同一份值在
 * `web/src/lib/attachments.ts::IMAGE_LIMITS`；改默认必须三处同步。
 */
export const MAX_IMAGES_PER_MESSAGE = 20;

/**
 * M-21：单条消息图片**总字节**上限：镜像服务端 `config.py:168`
 * `attachment_max_message_image_bytes` 默认 200 MiB（口径同上）。
 */
export const MAX_MESSAGE_IMAGE_BYTES = 200 * 1024 * 1024;

/**
 * M-21：单条「数量 / 总字节」预检的纯判定（返回**拒绝原因**，`null` = 放行）。
 *
 * 第一性原理：预检是服务端规则的客户端礼貌副本 -- 判定口径单一来源是服务端
 * `resolve_image_limits`（本文件两个镜像常量），这里只做「超没超」的比较与文案，
 * 不另立数字。唯一调用点是 `app.addPendingImage`（全部图片入场的汇聚点）。
 */
export function checkMessageImageLimits(
  existingCount: number,
  existingBytes: number,
  incomingBytes: number,
): string | null {
  if (existingCount >= MAX_IMAGES_PER_MESSAGE) {
    return (
      `最多 ${String(MAX_IMAGES_PER_MESSAGE)} 张图片/条（服务端上限）：` +
      "先删掉正文里多余的 [Image #N] 标记再附图。"
    );
  }
  if (existingBytes + incomingBytes > MAX_MESSAGE_IMAGE_BYTES) {
    return (
      `单条消息图片总字节超过 ${String(MAX_MESSAGE_IMAGE_BYTES / (1024 * 1024))} MiB 上限` +
      "（服务端 attachment_max_message_image_bytes 同口径）：请换更小的图或删掉已附的图。"
    );
  }
  return null;
}

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
      // 按**调用方给的** platform 判定路径语义：`fileURLToPath(url)` 只看运行时平台
      // （本函数签名把 platform 显式暴露出来就是为了让它可注入），Node >=22.1 的
      // `windows` 选项才让二者一致（#830 D3：Windows 上 `file:///tmp/x` 会抛错，
      // 而调用方其实要的是 Linux 语义）。
      return fileURLToPath(raw, { windows: platform === "win32" });
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
 * 读一张图片文件：`ok:false` 给出**明确原因**（`missing` 文件不在 / 读不了或不是常规文件，
 * `unsupported` 按 magic bytes 判不是本仓接受的图片，`too_large` 超过 `MAX_IMAGE_BYTES`），
 * 调用方据此给用户可读错误。
 *
 * 读盘在**同一个 fd** 上完成（#827 独立审查 P3/P4）：
 * - `statSync(...).isFile()` 先做非阻塞筛查（`openSync` 落在无写端的 FIFO 上会阻塞），
 *   非常规文件直接按 `missing` 拒；
 * - 之后 `openSync` 拿 fd，判定与读字节都用这个 fd（`fstatSync(fd)` / `readFileSync(fd)`），
 *   不再二次解析路径，关掉 `statSync` 与读盘之间「文件被换成 FIFO/目录」的 TOCTOU 窗口；
 * - `fstatSync(fd).size` 做尺寸预检：超限即拒，不读进内存。
 * 符号链接照常跟随（fd 指向目标），普通图片的可观察行为逐字不变。
 */
export function readImageFile(filePath: string): ReadImageFileResult {
  try {
    if (!statSync(filePath).isFile()) {
      return { ok: false, reason: "missing" };
    }
  } catch {
    return { ok: false, reason: "missing" };
  }
  let fd: number;
  try {
    fd = openSync(filePath, "r");
  } catch {
    return { ok: false, reason: "missing" };
  }
  try {
    const stats = fstatSync(fd);
    if (!stats.isFile()) {
      return { ok: false, reason: "missing" };
    }
    if (stats.size > MAX_IMAGE_BYTES) {
      return { ok: false, reason: "too_large" };
    }
    const bytes = readFileSync(fd);
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
  } catch {
    return { ok: false, reason: "missing" };
  } finally {
    closeSync(fd);
  }
}
