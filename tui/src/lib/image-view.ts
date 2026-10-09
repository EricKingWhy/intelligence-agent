/**
 * 终端能力 -> 待发图片的渲染决定（AC6 / AC7）。
 *
 * **不复制任何一家上游**：判定是 REUSE -- 直接用 pi-tui 既有的
 * `getCapabilities()` / `Image` 组件 / `imageFallback()`（`SPEC_ROOT/13_OPEN_SOURCE_REUSE_MATRIX.md`
 * 的 REUSE 档；本票票面也写死"按既有 pi-tui 能力"）。本文件只做「能力位 -> 组件 or
 * 文本占位」这一个分派，不重写协议编码。
 *
 * AC6 的判定依据是**上游事实**（阶段一实读）：`pi-tui/dist/terminal-image.js:68-70`
 * 在 `WT_SESSION` 下自报 `{images: null}` -- Windows Terminal 不支持 inline image
 * 协议（要 SIXEL，pi-tui 不支持），所以那里**必须**退化为文本占位，绝不能渲染假缩略图。
 */
import {
  Image,
  getCapabilities,
  getImageDimensions,
  imageFallback,
  type Component,
} from "@earendil-works/pi-tui";

import type { PendingImage } from "./pending-images.ts";

export interface DraftImageRenderers {
  /** pi-tui `Image` 组件的降级文字色（协议不可用时由它着色，AC6）。 */
  fallbackColor: (text: string) => string;
}

export type DraftImageRender =
  | { kind: "image"; component: Component }
  | { kind: "text"; lines: string[] };

const MAX_THUMBNAIL_WIDTH_CELLS = 40;

/**
 * 一张待发图片的渲染决定：终端支持图片协议 => pi-tui `Image` 缩略图（AC7）；
 * 否则 => `imageFallback()` 文本占位（文件名 + 尺寸 + 可打开的原图路径，AC6）。
 */
export function renderDraftImage(
  image: PendingImage,
  renderers: DraftImageRenderers,
): DraftImageRender {
  const base64 = Buffer.from(image.bytes).toString("base64");
  const dimensions = getImageDimensions(base64, image.mimeType);
  const capabilities = getCapabilities();
  if (capabilities.images !== null) {
    return {
      kind: "image",
      component: new Image(
        base64,
        image.mimeType,
        { fallbackColor: renderers.fallbackColor },
        { maxWidthCells: MAX_THUMBNAIL_WIDTH_CELLS, filename: image.path ?? image.name },
        dimensions ?? undefined,
      ),
    };
  }
  return { kind: "text", lines: [imageFallback(image.mimeType, dimensions ?? undefined, displayPath(image))] };
}

/**
 * 占位里出现的路径：有真实文件路径就给真路径（`imageFallback` 会把绝对路径缩短成
 * `~/...`，并在支持 OSC 8 的终端上链到 `file://`，这正是 AC6 要的"可打开的原图路径"）；
 * 剪贴板字节没有路径，只能用文件名，**不编造**一个假路径。
 */
function displayPath(image: PendingImage): string {
  return image.path ?? image.name;
}
